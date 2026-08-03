"""Stop-after-pass gate — AHE §8 (Path C / LRM-967).

Distinct from SHELVED PreCompletion (pre-finish checklist) and FailFast
(weak validation false-pass). Fires when an evaluator-style check has already
passed and the agent keeps writing/editing/building without new failing
evidence — the polish-after-pass regression mode.
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

STATE_KEY = "stop_after_pass_gate_state"
logger = logging.getLogger(__name__)

EVAL_STYLE_RE = re.compile(
    r"\b(?:pytest\b|python3?\s+[^\s;|&]*test_[^\s;|&]*\.py|"
    r"python3?\s+-m\s+pytest)\b",
    re.IGNORECASE,
)
PASS_SIGNAL_RE = re.compile(
    r"\b(?:passed|PASSED|ALL TESTS PASSED|ok\b|success(?:ful)?\b|"
    r"\d+\s+passed)\b",
    re.IGNORECASE,
)
FAIL_SIGNAL_RE = re.compile(
    r"\b(?:FAILED|ERROR|AssertionError|Traceback|failed\b|\d+\s+failed)\b",
    re.IGNORECASE,
)
WRITE_OR_BUILD_RE = re.compile(
    r"(?:>>?|tee\b|install\b|cp\b|mv\b|cat\s*>|\bgcc\b|\bg\+\+|\bclang\b|\brustc\b|"
    r"\bmake\b|\bcargo\s+build|write_text|open\([^)]*['\"]w|"
    r"sed\s+-i|perl\s+-i|\bnano\b|\bvim?\b)",
    re.IGNORECASE,
)
_SIDECAR_CANDIDATES = (
    Path("/logs/agent/stop_after_pass.nudge.txt"),
    Path("stop_after_pass.nudge.txt"),
)


class StopAfterPassGateMiddleware(Middleware):
    """Nudge when agent polishes after an evaluator-style pass."""

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
            "eval_pass_seen": False,
            "polish_after_pass": False,
            "pass_iteration": -1,
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
        iteration = int(getattr(hook_input, "current_iteration", 0) or 0)

        if EVAL_STYLE_RE.search(command):
            if FAIL_SIGNAL_RE.search(output) or (exit_code not in (None, 0)):
                # New failing evidence — allow further edits; clear polish flag.
                state["eval_pass_seen"] = False
                state["polish_after_pass"] = False
                state["last_reason"] = "eval_failed_reset"
            elif PASS_SIGNAL_RE.search(output) and exit_code in (None, 0):
                state["eval_pass_seen"] = True
                state["pass_iteration"] = iteration
                state["last_reason"] = "eval_style_pass"

        if state["eval_pass_seen"] and WRITE_OR_BUILD_RE.search(command):
            # Editing after a recorded pass without a newer failing eval.
            state["polish_after_pass"] = True
            state["last_reason"] = "write_or_build_after_eval_pass"

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

        if not (state["eval_pass_seen"] and state["polish_after_pass"]):
            return HookResult.no_changes()

        reason = state.get("last_reason") or "polish_after_pass"
        state["nudge_count"] += 1
        state["last_nudge_iteration"] = iteration
        state["nudge_fired"] = True
        hook_input.agent_state.set_global_value(STATE_KEY, self._dump_state(state))

        reminder = (
            f"StopAfterPassGate: {reason}; iteration={iteration}. "
            "Evaluator-style checks already passed. Stop editing unless you have "
            "new failing evidence. Treat the current filesystem/service state as "
            "the publish state; extra polishing after a passing check is a "
            "regression risk. Do not invent domain tips from this reminder."
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
            "[StopAfterPassGateMiddleware] Nudge #%s reason=%s iteration=%s "
            "eval_pass=%s polish=%s system_patched=%s",
            state["nudge_count"],
            reason,
            iteration,
            state["eval_pass_seen"],
            state["polish_after_pass"],
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
                    "[StopAfterPassGateMiddleware] Appended fire sidecar %s", path
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
            "eval_pass_seen": bool(state.get("eval_pass_seen", False)),
            "polish_after_pass": bool(state.get("polish_after_pass", False)),
            "pass_iteration": int(state.get("pass_iteration", -1) or -1),
            "nudge_count": int(state.get("nudge_count", 0) or 0),
            "last_nudge_iteration": int(state.get("last_nudge_iteration", -10_000) or -10_000),
            "nudge_fired": bool(state.get("nudge_fired", False)),
            "last_reason": str(state.get("last_reason") or ""),
        }

    def _load_state(self, raw_state: Any) -> dict[str, Any]:
        return self._dump_state(raw_state if isinstance(raw_state, dict) else {})
