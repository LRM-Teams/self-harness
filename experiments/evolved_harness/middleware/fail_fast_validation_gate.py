"""Fail-fast custom-validation gate — AHE §2 (Path C / LRM-966).

Distinct from SHELVED PreCompletion (evaluator-style runner) and OfficialChecker
(task-named check.py). Fires when the agent’s *own* validation scripts/commands
fail open: mismatch signals ignored, `diff`/`cmp` swallowed with `|| true`, or
scripts that print `passed` / exit 0 after expected-vs-actual disagreement.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

from nexau.archs.main_sub.execution.hooks import (
    AfterToolHookInput,
    BeforeAgentHookInput,
    BeforeModelHookInput,
    HookResult,
    Middleware,
)
from nexau.core.messages import Message, Role, TextBlock

STATE_KEY = "fail_fast_validation_gate_state"
logger = logging.getLogger(__name__)

WRITE_OR_BUILD_RE = re.compile(
    r"(?:>>?|tee\b|install\b|cp\b|mv\b|cat\s*>|\bgcc\b|\bg\+\+|\bclang\b|\brustc\b|"
    r"\bmake\b|\bcargo\s+build|write_text|open\([^)]*['\"]w)",
    re.IGNORECASE,
)
DIFF_CMP_RE = re.compile(r"\b(?:diff|cmp)\b", re.IGNORECASE)
DIFF_SWALLOWED_RE = re.compile(
    r"\b(?:diff|cmp)\b[\s\S]{0,120}(?:\|\|\s*true|\|\|\s*:|;\s*true\b)",
    re.IGNORECASE,
)
VALIDATION_SCRIPT_WRITE_RE = re.compile(
    r"(?:cat\s*>|tee\b|>>?)\s*([^\s;|&]+(?:check|validat|verify|test|compare)[^\s;|&]*\.(?:sh|bash|py))",
    re.IGNORECASE,
)
HEREDOC_SCRIPT_RE = re.compile(
    r"cat\s*>\s*([^\s;|&]+\.(?:sh|bash))\s*<<",
    re.IGNORECASE,
)
RUN_SCRIPT_RE = re.compile(
    r"\b(?:bash|sh)\s+([^\s;|&]+\.(?:sh|bash))\b|\bpython3?\s+([^\s;|&]*(?:check|validat|verify|test|compare)[^\s;|&]*\.py)\b",
    re.IGNORECASE,
)
SET_E_RE = re.compile(r"\bset\s+-[a-zA-Z]*e", re.IGNORECASE)
MISMATCH_SIGNAL_RE = re.compile(
    r"(?:differ|mismatch|expected|actual|AssertionError|FAILED|Traceback|"
    r"Files\s+\S+\s+and\s+\S+\s+differ|cmp:\s|Only in\s)",
    re.IGNORECASE,
)
PASS_SIGNAL_RE = re.compile(
    r"\b(?:passed|PASSED|ALL TESTS PASSED|ok\b|success(?:ful)?\b)\b",
    re.IGNORECASE,
)
_SIDECAR_CANDIDATES = (
    Path("/logs/agent/fail_fast_validation.nudge.txt"),
    Path("fail_fast_validation.nudge.txt"),
)


class FailFastValidationGateMiddleware(Middleware):
    """Nudge when custom validation fails open (mismatch treated as pass)."""

    def __init__(
        self,
        *,
        min_iterations_before_nudge: int = 3,
        renudge_every_iterations: int = 8,
        max_nudges: int = 3,
    ) -> None:
        self.min_iterations_before_nudge = int(min_iterations_before_nudge)
        self.renudge_every_iterations = int(renudge_every_iterations)
        self.max_nudges = int(max_nudges)

    def before_agent(self, hook_input: BeforeAgentHookInput) -> HookResult:
        state = {
            "wrote_or_built": False,
            "weak_validation_seen": False,
            "false_pass_seen": False,
            "scripts_without_set_e": [],
            "nudge_count": 0,
            "last_nudge_iteration": -10_000,
            "nudge_fired": False,
            "last_reason": "",
        }
        hook_input.agent_state.set_global_value(STATE_KEY, self._dump_state(state))
        return HookResult.no_changes()

    def after_tool(self, hook_input: AfterToolHookInput) -> HookResult:
        if hook_input.tool_name != "run_shell_command":
            return HookResult.no_changes()

        tool_input = hook_input.tool_input if isinstance(hook_input.tool_input, dict) else {}
        command = str(tool_input.get("command", ""))
        output = self._tool_output_text(hook_input)
        exit_code = self._extract_exit_code(hook_input)
        state = self._load_state(hook_input.agent_state.get_global_value(STATE_KEY, {}))

        if WRITE_OR_BUILD_RE.search(command):
            state["wrote_or_built"] = True

        scripts = set(state.get("scripts_without_set_e") or [])
        for match in VALIDATION_SCRIPT_WRITE_RE.finditer(command):
            path = match.group(1)
            if not SET_E_RE.search(command):
                scripts.add(path)
                state["weak_validation_seen"] = True
                state["last_reason"] = f"validation_script_without_set_e:{path}"
        for match in HEREDOC_SCRIPT_RE.finditer(command):
            path = match.group(1)
            # Heredoc body may include set -e later; only flag when body lacks it.
            body = command[match.end() :]
            if not SET_E_RE.search(body) and not SET_E_RE.search(command):
                scripts.add(path)
                state["weak_validation_seen"] = True
                state["last_reason"] = f"script_heredoc_without_set_e:{path}"

        if DIFF_SWALLOWED_RE.search(command):
            state["weak_validation_seen"] = True
            state["false_pass_seen"] = True
            state["last_reason"] = "diff_or_cmp_swallowed_with_true"

        if DIFF_CMP_RE.search(command) and exit_code not in (None, 0):
            # Explicit non-zero from diff/cmp is healthy fail-fast — not a fire.
            pass
        elif DIFF_CMP_RE.search(command) and exit_code == 0 and MISMATCH_SIGNAL_RE.search(output):
            state["weak_validation_seen"] = True
            state["false_pass_seen"] = True
            state["last_reason"] = "diff_cmp_exit0_with_mismatch_output"

        if MISMATCH_SIGNAL_RE.search(output) and PASS_SIGNAL_RE.search(output):
            state["weak_validation_seen"] = True
            state["false_pass_seen"] = True
            state["last_reason"] = "mismatch_output_then_passed_signal"

        if MISMATCH_SIGNAL_RE.search(output) and exit_code == 0 and DIFF_CMP_RE.search(command):
            state["weak_validation_seen"] = True
            state["false_pass_seen"] = True
            state["last_reason"] = "mismatch_with_exit0"

        for match in RUN_SCRIPT_RE.finditer(command):
            path = match.group(1) or match.group(2)
            if path and path in scripts:
                state["weak_validation_seen"] = True
                if PASS_SIGNAL_RE.search(output) or exit_code == 0:
                    state["false_pass_seen"] = True
                    state["last_reason"] = f"ran_weak_script_as_pass:{path}"

        state["scripts_without_set_e"] = sorted(scripts)
        hook_input.agent_state.set_global_value(STATE_KEY, self._dump_state(state))
        return HookResult.no_changes()

    def before_model(self, hook_input: BeforeModelHookInput) -> HookResult:
        iteration = hook_input.current_iteration
        if iteration < self.min_iterations_before_nudge:
            return HookResult.no_changes()

        has_assistant = any(
            getattr(message, "role", None) == Role.ASSISTANT for message in hook_input.messages
        )
        if not has_assistant:
            return HookResult.no_changes()

        state = self._load_state(hook_input.agent_state.get_global_value(STATE_KEY, {}))
        if state["nudge_count"] >= self.max_nudges:
            return HookResult.no_changes()
        if (
            state["nudge_count"] > 0
            and iteration - state["last_nudge_iteration"] < self.renudge_every_iterations
        ):
            return HookResult.no_changes()

        if not state["wrote_or_built"]:
            return HookResult.no_changes()
        if not (state["false_pass_seen"] or state["weak_validation_seen"]):
            return HookResult.no_changes()
        # Prefer firmer false-pass evidence; allow weak_validation alone late.
        if not state["false_pass_seen"] and iteration < max(
            self.min_iterations_before_nudge + 6, 10
        ):
            return HookResult.no_changes()

        reason = state.get("last_reason") or "weak_custom_validation"
        state["nudge_count"] += 1
        state["last_nudge_iteration"] = iteration
        state["nudge_fired"] = True
        hook_input.agent_state.set_global_value(STATE_KEY, self._dump_state(state))

        reminder = (
            f"FailFastValidationGate: {reason}; iteration={iteration}. "
            "Custom validation scripts must fail fast: use `set -e` (or explicit "
            "non-zero exits) for every diff/cmp/assertion. If expected-vs-actual "
            "mismatch appears in the output, treat the validation as **failed** "
            "even if the script later prints `passed` or exits 0. Do not swallow "
            "`diff`/`cmp` with `|| true`. Fix the deliverable against the real "
            "failure — do not invent domain tips from this reminder."
        )
        self._write_sidecar(reminder)

        updated_messages: list[Message] = []
        system_patched = False
        for message in hook_input.messages:
            if not system_patched and getattr(message, "role", None) == Role.SYSTEM:
                existing = ""
                get_text = getattr(message, "get_text_content", None)
                if callable(get_text):
                    existing = get_text() or ""
                else:
                    existing = self._message_text(message)
                if reminder not in existing:
                    patched = (existing + "\n\n" + reminder) if existing else reminder
                    updated_messages.append(
                        Message(role=Role.SYSTEM, content=[TextBlock(text=patched)])
                    )
                else:
                    updated_messages.append(message)
                system_patched = True
            else:
                updated_messages.append(message)
        updated_messages.append(Message(role=Role.USER, content=[TextBlock(text=reminder)]))
        logger.info(
            "[FailFastValidationGateMiddleware] Nudge #%s reason=%s iteration=%s "
            "false_pass=%s weak=%s system_patched=%s",
            state["nudge_count"],
            reason,
            iteration,
            state["false_pass_seen"],
            state["weak_validation_seen"],
            system_patched,
        )
        return HookResult.with_modifications(messages=updated_messages)

    def _write_sidecar(self, text: str) -> None:
        for path in _SIDECAR_CANDIDATES:
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                with path.open("a", encoding="utf-8") as fh:
                    fh.write(text.rstrip() + "\n")
                logger.info(
                    "[FailFastValidationGateMiddleware] Appended fire sidecar %s", path
                )
                return
            except OSError:
                continue

    def _message_text(self, message: Any) -> str:
        content = getattr(message, "content", None)
        if isinstance(content, str):
            return content
        if not content:
            return ""
        parts: list[str] = []
        for block in content:
            text = getattr(block, "text", None)
            if isinstance(text, str):
                parts.append(text)
        return "\n".join(parts)

    def _tool_output_text(self, hook_input: AfterToolHookInput) -> str:
        for attr in ("tool_output", "tool_result", "result", "output"):
            value = getattr(hook_input, attr, None)
            if value is None:
                continue
            if isinstance(value, str):
                return value
            if isinstance(value, dict):
                for key in ("output", "content", "stdout", "stderr"):
                    text = value.get(key)
                    if isinstance(text, str):
                        return text
                return str(value)
            text = getattr(value, "output", None) or getattr(value, "content", None)
            if isinstance(text, str):
                return text
            return str(value)
        return ""

    def _extract_exit_code(self, hook_input: AfterToolHookInput) -> int | None:
        for attr in ("tool_output", "tool_result", "result", "output"):
            value = getattr(hook_input, attr, None)
            if isinstance(value, dict) and "exit_code" in value:
                try:
                    return int(value["exit_code"])
                except (TypeError, ValueError):
                    return None
            if value is not None:
                code = getattr(value, "exit_code", None)
                if code is not None:
                    try:
                        return int(code)
                    except (TypeError, ValueError):
                        return None
        return None

    def _dump_state(self, state: dict[str, Any]) -> dict[str, Any]:
        return {
            "wrote_or_built": bool(state.get("wrote_or_built", False)),
            "weak_validation_seen": bool(state.get("weak_validation_seen", False)),
            "false_pass_seen": bool(state.get("false_pass_seen", False)),
            "scripts_without_set_e": list(state.get("scripts_without_set_e") or []),
            "nudge_count": int(state.get("nudge_count", 0) or 0),
            "last_nudge_iteration": int(state.get("last_nudge_iteration", -10_000) or -10_000),
            "nudge_fired": bool(state.get("nudge_fired", False)),
            "last_reason": str(state.get("last_reason") or ""),
        }

    def _load_state(self, raw_state: Any) -> dict[str, Any]:
        return self._dump_state(raw_state if isinstance(raw_state, dict) else {})
