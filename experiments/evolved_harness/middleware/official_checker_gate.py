"""Official-checker finalize gate — distinct from SHELVED execution_risk_hints.

Fires when the task names (or clearly exposes) an official acceptance entry
(check.py / eval.py / test_outputs.py / pytest) but the agent is about to
finish without having actually run that entry after writing contract
deliverables. Soft nudge + durable sidecar; not a kitchen-sink risk pack.
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

STATE_KEY = "official_checker_gate_state"
logger = logging.getLogger(__name__)

DELIVERABLE_PATH_RE = re.compile(r"(/app/[A-Za-z0-9_./-]+)")
WRITE_CMD_RE = re.compile(
    r"(?:>|tee\b|cp\b|mv\b|install\b|write_text|open\([^)]*['\"]w|"
    r"\.save\(|torch\.save|pickle\.dump|joblib\.dump)",
    re.IGNORECASE,
)
REQUIRED_HINT_RE = re.compile(
    r"(?:stored in|write(?:n)? to|save(?:d)? (?:to|as)|output(?: file)?|file titled|"
    r"JSON file called|save your solution in the file|/app/)"
    r"[^\n]{0,60}?(/app/[A-Za-z0-9_./-]+)",
    re.IGNORECASE,
)
# Task-named official checkers (AHE mirror-evaluator surface).
NAMED_CHECKER_RE = re.compile(
    r"\b((?:check|eval|verify|validate|test_outputs)(?:\.py)?)\b",
    re.IGNORECASE,
)
PROVIDED_CHECKER_RE = re.compile(
    r"(?:provided|official|use the|look at the)\s+([A-Za-z0-9_./-]+\.py)",
    re.IGNORECASE,
)
PYTEST_RUN_RE = re.compile(r"\bpytest\b", re.IGNORECASE)
PYTHON_CHECKER_RUN_RE = re.compile(
    r"\bpython3?\s+[^\n]*\b((?:check|eval|verify|validate|test_outputs)\.py)\b",
    re.IGNORECASE,
)
SELF_PROXY_RE = re.compile(
    r"\b(?:my_?test|tmp_?check|validate_local|self_?check|scratch_?test)\b",
    re.IGNORECASE,
)

_DEFAULT_EVALUATOR_CANDIDATES = (
    "check.py",
    "eval.py",
    "test_outputs.py",
)

_SIDECAR_CANDIDATES = (
    Path("/logs/agent/official_checker.nudge.txt"),
    Path("official_checker.nudge.txt"),
)


class OfficialCheckerGateMiddleware(Middleware):
    """Require a real run of the official checker before finalize."""

    def __init__(
        self,
        *,
        min_iterations_before_nudge: int = 3,
        renudge_every_iterations: int = 8,
        max_nudges: int = 3,
        include_default_evaluator_candidates: bool = True,
    ) -> None:
        self.min_iterations_before_nudge = int(min_iterations_before_nudge)
        self.renudge_every_iterations = int(renudge_every_iterations)
        self.max_nudges = int(max_nudges)
        self.include_default_evaluator_candidates = bool(
            include_default_evaluator_candidates
        )

    def before_agent(self, hook_input: BeforeAgentHookInput) -> HookResult:
        state = self._load_state(hook_input.agent_state.get_global_value(STATE_KEY, {}))
        required: set[str] = set()
        checkers: set[str] = set()
        for message in hook_input.messages:
            if getattr(message, "role", None) != Role.USER:
                continue
            text = self._message_text(message)
            required.update(DELIVERABLE_PATH_RE.findall(text))
            for match in REQUIRED_HINT_RE.finditer(text):
                required.add(match.group(1).rstrip(".,;)'\""))
            for match in NAMED_CHECKER_RE.finditer(text):
                name = match.group(1)
                if not name.endswith(".py"):
                    name = f"{name}.py"
                checkers.add(name.lower())
            for match in PROVIDED_CHECKER_RE.finditer(text):
                checkers.add(match.group(1).rsplit("/", 1)[-1].lower())

        if self.include_default_evaluator_candidates and required:
            # Portable AHE default when contract paths exist but the prompt is
            # quiet about the runner — still prefer pytest / test_outputs.py.
            checkers.update(_DEFAULT_EVALUATOR_CANDIDATES)

        state["required_paths"] = required
        state["checker_names"] = checkers
        state["written_paths"] = set()
        state["checker_runs"] = 0
        state["pytest_runs"] = 0
        state["proxy_runs"] = 0
        state["nudge_count"] = 0
        state["last_nudge_iteration"] = -10_000
        state["nudge_fired"] = False
        hook_input.agent_state.set_global_value(STATE_KEY, self._dump_state(state))
        if checkers or required:
            logger.info(
                "[OfficialCheckerGateMiddleware] checkers=%s required=%s",
                sorted(checkers),
                sorted(required),
            )
        return HookResult.no_changes()

    def after_tool(self, hook_input: AfterToolHookInput) -> HookResult:
        if hook_input.tool_name != "run_shell_command":
            return HookResult.no_changes()
        tool_input = hook_input.tool_input if isinstance(hook_input.tool_input, dict) else {}
        command = str(tool_input.get("command", ""))
        state = self._load_state(hook_input.agent_state.get_global_value(STATE_KEY, {}))

        for match in DELIVERABLE_PATH_RE.finditer(command):
            path = match.group(1).rstrip(".,;)'\"")
            if WRITE_CMD_RE.search(command):
                state["written_paths"].add(path)

        if PYTEST_RUN_RE.search(command):
            state["pytest_runs"] = int(state.get("pytest_runs", 0) or 0) + 1
            state["checker_runs"] = int(state.get("checker_runs", 0) or 0) + 1
        for match in PYTHON_CHECKER_RUN_RE.finditer(command):
            name = match.group(1).lower()
            if name in state["checker_names"] or name in _DEFAULT_EVALUATOR_CANDIDATES:
                state["checker_runs"] = int(state.get("checker_runs", 0) or 0) + 1
        # Bare invocation of a named checker path also counts.
        for name in state["checker_names"]:
            if re.search(rf"\b{re.escape(name)}\b", command, re.IGNORECASE):
                if re.search(r"\b(?:python3?|pytest)\b", command, re.IGNORECASE):
                    state["checker_runs"] = int(state.get("checker_runs", 0) or 0) + 1
                    break
        if SELF_PROXY_RE.search(command):
            state["proxy_runs"] = int(state.get("proxy_runs", 0) or 0) + 1

        hook_input.agent_state.set_global_value(STATE_KEY, self._dump_state(state))
        return HookResult.no_changes()

    def before_model(self, hook_input: BeforeModelHookInput) -> HookResult:
        iteration = int(hook_input.current_iteration or 0)
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

        if not state["checker_names"] and not state["required_paths"]:
            return HookResult.no_changes()

        missing = sorted(state["required_paths"] - state["written_paths"])
        checker_runs = int(state.get("checker_runs", 0) or 0)
        written = bool(state["written_paths"] & state["required_paths"]) or (
            not state["required_paths"] and bool(state["written_paths"])
        )

        reason = None
        if missing and iteration >= self.min_iterations_before_nudge + 2:
            reason = f"contract_paths_unwritten:{','.join(missing[:3])}"
        elif written and checker_runs == 0:
            reason = "deliverable_written_without_official_checker"
        elif checker_runs == 0 and iteration >= max(10, self.min_iterations_before_nudge * 3):
            reason = "no_official_checker_run_yet"
        elif (
            int(state.get("proxy_runs", 0) or 0) > 0
            and checker_runs == 0
            and written
        ):
            reason = "self_proxy_without_official_checker"

        if reason is None:
            return HookResult.no_changes()

        state["nudge_count"] += 1
        state["last_nudge_iteration"] = iteration
        state["nudge_fired"] = True
        hook_input.agent_state.set_global_value(STATE_KEY, self._dump_state(state))

        checkers = ", ".join(sorted(state["checker_names"])[:4]) or "pytest -q test_outputs.py"
        paths = ", ".join(sorted(state["required_paths"])[:4]) or "/app/<required>"
        reminder = (
            f"OfficialCheckerGate: {reason}; iteration={iteration}. "
            f"Contract deliverable path(s): {paths}. "
            f"Run the official acceptance entry now ({checkers}) — prefer "
            "`pytest -q <testfile>` or `python <check.py>` named by the task. "
            "Do not treat a self-written proxy, silent `python test_*.py` with empty "
            "output, or file-existence alone as final proof. Fix against the official "
            "failure output, then stop."
        )
        self._write_sidecar(reminder)
        # Tess TWEAK `1122`: cleaned InMemoryTracer drops FRAMEWORK, so a
        # FRAMEWORK-only inject never yields literal `OfficialCheckerGate:` hits.
        # Dual-land like LocalContext t2 — patch SYSTEM (survives as
        # `system_prompt` in cleaned.json) + keep FRAMEWORK for the live model.
        updated_messages: list[Message] = []
        system_patched = False
        for message in hook_input.messages:
            if (
                not system_patched
                and getattr(message, "role", None) == Role.SYSTEM
            ):
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
        updated_messages.append(Message(role=Role.FRAMEWORK, content=[TextBlock(text=reminder)]))
        logger.info(
            "[OfficialCheckerGateMiddleware] Nudge #%s reason=%s iteration=%s "
            "system_patched=%s",
            state["nudge_count"],
            reason,
            iteration,
            system_patched,
        )
        return HookResult.with_modifications(messages=updated_messages)

    def _write_sidecar(self, text: str) -> None:
        for path in _SIDECAR_CANDIDATES:
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                with path.open("a", encoding="utf-8") as fh:
                    fh.write(text.rstrip() + "\n")
                logger.info("[OfficialCheckerGateMiddleware] Appended fire sidecar %s", path)
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

    def _load_state(self, raw_state: Any) -> dict[str, Any]:
        state = raw_state if isinstance(raw_state, dict) else {}

        def _as_set(value: Any) -> set[str]:
            if isinstance(value, set):
                return {str(v) for v in value}
            if isinstance(value, (list, tuple)):
                return {str(v) for v in value}
            return set()

        return {
            "required_paths": _as_set(state.get("required_paths")),
            "checker_names": {str(v).lower() for v in _as_set(state.get("checker_names"))},
            "written_paths": _as_set(state.get("written_paths")),
            "checker_runs": int(state.get("checker_runs", 0) or 0),
            "pytest_runs": int(state.get("pytest_runs", 0) or 0),
            "proxy_runs": int(state.get("proxy_runs", 0) or 0),
            "nudge_count": int(state.get("nudge_count", 0) or 0),
            "last_nudge_iteration": int(state.get("last_nudge_iteration", -10_000) or -10_000),
            "nudge_fired": bool(state.get("nudge_fired", False)),
        }

    def _dump_state(self, state: dict[str, Any]) -> dict[str, Any]:
        return {
            "required_paths": sorted(state.get("required_paths", set())),
            "checker_names": sorted(state.get("checker_names", set())),
            "written_paths": sorted(state.get("written_paths", set())),
            "checker_runs": int(state.get("checker_runs", 0) or 0),
            "pytest_runs": int(state.get("pytest_runs", 0) or 0),
            "proxy_runs": int(state.get("proxy_runs", 0) or 0),
            "nudge_count": int(state.get("nudge_count", 0) or 0),
            "last_nudge_iteration": int(state.get("last_nudge_iteration", -10_000) or -10_000),
            "nudge_fired": bool(state.get("nudge_fired", False)),
        }
