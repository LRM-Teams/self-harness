"""Generalize-not-overfit gate — AHE §5 (Path C / LRM-991).

Distinct from SHELVED SemanticNotProxy (proxy metric ≠ contract),
OfficialChecker, FailFast, StopAfterPass, LiteralContract, RereadDeliverable,
and PersistBest. Fires when the agent has done single-sample / single-instance
validation (or written a deliverable after one happy-path check) but shows no
second-case, perturbation, boundary, or black-box re-check before finishing.

Process pressure only — never leak task-domain recipes or fixture answers.
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

STATE_KEY = "generalize_not_overfit_gate_state"
MARKER = "GeneralizeNotOverfitGate:"
logger = logging.getLogger(__name__)

DELIVERABLE_PATH_RE = re.compile(r"(/app/[A-Za-z0-9_./-]+)")
WRITE_CMD_RE = re.compile(
    r"(?:>|tee\b|cp\b|mv\b|install\b|write_text|open\([^)]*['\"]w|"
    r"\.save\(|torch\.save|pickle\.dump|joblib\.dump|json\.dump|to_csv|to_json)",
    re.IGNORECASE,
)
# Single-instance / happy-path validation signals.
SINGLE_CASE_RE = re.compile(
    r"(?:"
    r"\bpytest\b|"
    r"\bpython3?\s+[^\n]*(?:test_|check_|verify_|validate_|eval_)[A-Za-z0-9_]+\.py\b|"
    r"\bpython3?\s+-c\s+['\"][^'\"]{0,200}(?:assert|test|check|verify)|"
    r"\b(?:assert|diff|cmp)\b"
    r")",
    re.IGNORECASE,
)
# Multi-case / boundary / perturbation / black-box re-check signals (process-level).
GENERALIZE_RE = re.compile(
    r"(?:"
    r"\bboundary\b|\bedge[-_ ]?case\b|\bperturb|"
    r"\bhold[-_ ]?out\b|\bunseen\b|\bfresh[-_ ]?instance\b|"
    r"\bsecond[-_ ]?case\b|\banother[-_ ]?(?:example|case|instance|input)\b|"
    r"\bmultiple[-_ ]?cases\b|\bhidden[-_ ]?instance\b|"
    r"\bcross[-_ ]?valid|\bk[-_ ]?fold\b|"
    r"\bblack[-_ ]?box\b|\bgeneraliz|"
    r"\bpytest\b[^\n]*(?:-k\b|[^\n]+\.py[^\n]+\.py)|"
    r"\bfor\s+\w+\s+in\b[^\n]{0,80}(?:case|sample|example|input|seed)|"
    r"\b(?:seq|range)\s*\([^\)]*\)[^\n]{0,60}(?:test|check|eval|verify)"
    r")",
    re.IGNORECASE,
)
INPUT_PATH_RE = re.compile(
    r"(?:(?:<|cat|head|tail|open|read_text|load|np\.load|json\.load)\s*[('\"]?)"
    r"(/app/[A-Za-z0-9_./-]+|"
    r"(?:(?!/app/)[A-Za-z0-9_./-]+\.(?:txt|json|csv|bin|dat|npy|pt|pth|xml|yaml|yml|fen|pgn)))",
    re.IGNORECASE,
)

_SIDECAR_CANDIDATES = (
    Path("/logs/agent/generalize_not_overfit.nudge.txt"),
    Path("generalize_not_overfit.nudge.txt"),
)


class GeneralizeNotOverfitGateMiddleware(Middleware):
    """Nudge when only single-case validation is seen before finish."""

    def __init__(
        self,
        *,
        min_iterations_before_nudge: int = 3,
        renudge_every_iterations: int = 8,
        max_nudges: int = 3,
        min_single_case_events: int = 1,
    ) -> None:
        self.min_iterations_before_nudge = int(min_iterations_before_nudge)
        self.renudge_every_iterations = int(renudge_every_iterations)
        self.max_nudges = int(max_nudges)
        self.min_single_case_events = int(min_single_case_events)

    def before_agent(self, hook_input: BeforeAgentHookInput) -> HookResult:
        state = {
            "single_case_events": 0,
            "generalize_events": 0,
            "write_events": 0,
            "eval_inputs": [],
            "nudge_count": 0,
            "last_nudge_iteration": -10_000,
            "nudge_fired": False,
            "last_reason": "",
            "sticky_reminder": "",
        }
        hook_input.agent_state.set_global_value(STATE_KEY, self._dump_state(state))
        return HookResult.no_changes()

    def after_tool(self, hook_input: AfterToolHookInput) -> HookResult:
        if hook_input.tool_name != "run_shell_command":
            return HookResult.no_changes()

        tool_input = hook_input.tool_input if isinstance(hook_input.tool_input, dict) else {}
        command = str(tool_input.get("command", ""))
        state = self._load_state(hook_input.agent_state.get_global_value(STATE_KEY, {}))
        inputs = set(state["eval_inputs"])

        if WRITE_CMD_RE.search(command):
            state["write_events"] = int(state["write_events"]) + 1
            state["last_reason"] = "deliverable_write_after_single_case_risk"

        if GENERALIZE_RE.search(command):
            state["generalize_events"] = int(state["generalize_events"]) + 1
            state["last_reason"] = "generalize_evidence"
        elif SINGLE_CASE_RE.search(command):
            state["single_case_events"] = int(state["single_case_events"]) + 1
            state["last_reason"] = "single_case_validation"

        for match in INPUT_PATH_RE.finditer(command):
            path = match.group(1).rstrip(".,;)'\"")
            if path:
                inputs.add(path)
        # Distinct eval inputs ≥2 counts as generalize evidence.
        if len(inputs) >= 2 and int(state["generalize_events"]) == 0:
            state["generalize_events"] = 1
            state["last_reason"] = "multi_input_validation"

        state["eval_inputs"] = sorted(inputs)
        hook_input.agent_state.set_global_value(STATE_KEY, self._dump_state(state))
        return HookResult.no_changes()

    def before_model(self, hook_input: BeforeModelHookInput) -> HookResult:
        iteration = int(hook_input.current_iteration or 0)
        state = self._load_state(hook_input.agent_state.get_global_value(STATE_KEY, {}))
        sticky = str(state.get("sticky_reminder") or "")
        messages = list(hook_input.messages)
        sticky_applied = False

        # Prefer SYSTEM PREPEND/INSERT + first-USER PREPEND + sticky re-paint so
        # cleaned tracers retain the literal marker (PersistBest TWEAK lesson).
        if sticky and not self._messages_have_marker(messages, MARKER):
            messages, sys_ok = self._apply_system_marker(messages, sticky)
            messages, user_ok = self._apply_user_marker(messages, sticky)
            sticky_applied = sys_ok or user_ok
            if sticky_applied:
                logger.info(
                    "[GeneralizeNotOverfitGateMiddleware] Sticky re-apply "
                    "iteration=%s system=%s user=%s",
                    iteration,
                    sys_ok,
                    user_ok,
                )

        def _passthrough() -> HookResult:
            if sticky_applied:
                return HookResult.with_modifications(messages=messages)
            return HookResult.no_changes()

        if iteration < self.min_iterations_before_nudge:
            return _passthrough()

        has_assistant = any(
            getattr(message, "role", None) == Role.ASSISTANT for message in messages
        )
        if not has_assistant:
            return _passthrough()

        if state["nudge_count"] >= self.max_nudges:
            return _passthrough()
        if (
            state["nudge_count"] > 0
            and iteration - state["last_nudge_iteration"] < self.renudge_every_iterations
        ):
            return _passthrough()

        single = int(state["single_case_events"])
        generalize = int(state["generalize_events"])
        writes = int(state["write_events"])
        # Fire when single-case validation happened (or write after thin check)
        # and no generalize evidence yet.
        if generalize > 0:
            return _passthrough()
        if single < self.min_single_case_events and writes < 1:
            return _passthrough()
        if single < self.min_single_case_events and writes >= 1:
            # Write without any check is weak; still nudge once agents are deep
            # enough — overfit-to-visible-sample often skips a second case.
            state["last_reason"] = state.get("last_reason") or "write_without_second_case"

        reason = state.get("last_reason") or "single_case_without_generalize_check"
        state["nudge_count"] += 1
        state["last_nudge_iteration"] = iteration
        state["nudge_fired"] = True

        inputs_s = ", ".join((state.get("eval_inputs") or [])[:4]) or "<none>"
        reminder = (
            f"{MARKER} {reason}; iteration={iteration}; "
            f"single_case_events={single}; generalize_events={generalize}; "
            f"write_events={writes}; eval_inputs={inputs_s}. "
            "Do not overfit to the one visible sample or a single happy-path "
            "check. Before finishing, run at least one more evaluator-facing "
            "check on a second case, perturbation, boundary, or fresh instance "
            "(black-box when internals are unavailable). Do not invent domain "
            "tips or fixture answers from this reminder."
        )
        self._write_sidecar(reminder)
        state["sticky_reminder"] = reminder
        hook_input.agent_state.set_global_value(STATE_KEY, self._dump_state(state))

        updated_messages, system_patched = self._apply_system_marker(messages, reminder)
        updated_messages, user_patched = self._apply_user_marker(
            updated_messages, reminder
        )
        updated_messages.append(Message(role=Role.USER, content=[TextBlock(text=reminder)]))
        logger.info(
            "[GeneralizeNotOverfitGateMiddleware] Nudge #%s reason=%s iteration=%s "
            "single=%s generalize=%s writes=%s system_patched=%s user_patched=%s",
            state["nudge_count"],
            reason,
            iteration,
            single,
            generalize,
            writes,
            system_patched,
            user_patched,
        )
        return HookResult.with_modifications(messages=updated_messages)

    def _messages_have_marker(self, messages: list[Message], marker: str) -> bool:
        seen_user = False
        for message in messages:
            if self._is_system_role(message) and marker in self._message_text(message):
                return True
            if not seen_user and getattr(message, "role", None) == Role.USER:
                seen_user = True
                if marker in self._message_text(message):
                    return True
        return False

    def _apply_system_marker(
        self, messages: list[Message], reminder: str
    ) -> tuple[list[Message], bool]:
        updated_messages: list[Message] = []
        system_patched = False
        for message in messages:
            if not system_patched and self._is_system_role(message):
                existing = self._message_text(message)
                if MARKER not in existing:
                    patched = (reminder + "\n\n" + existing) if existing else reminder
                    updated_messages.append(
                        Message(role=Role.SYSTEM, content=[TextBlock(text=patched)])
                    )
                else:
                    updated_messages.append(message)
                system_patched = True
            else:
                updated_messages.append(message)
        if not system_patched:
            updated_messages.insert(
                0, Message(role=Role.SYSTEM, content=[TextBlock(text=reminder)])
            )
            system_patched = True
        return updated_messages, system_patched

    def _apply_user_marker(
        self, messages: list[Message], reminder: str
    ) -> tuple[list[Message], bool]:
        updated_messages: list[Message] = []
        user_patched = False
        for message in messages:
            if not user_patched and getattr(message, "role", None) == Role.USER:
                existing = self._message_text(message)
                if MARKER not in existing:
                    patched = (reminder + "\n\n" + existing) if existing else reminder
                    updated_messages.append(
                        Message(role=Role.USER, content=[TextBlock(text=patched)])
                    )
                else:
                    updated_messages.append(message)
                user_patched = True
            else:
                updated_messages.append(message)
        return updated_messages, user_patched

    def _write_sidecar(self, text: str) -> None:
        for path in _SIDECAR_CANDIDATES:
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                with path.open("a", encoding="utf-8") as fh:
                    fh.write(text.rstrip() + "\n")
                logger.info(
                    "[GeneralizeNotOverfitGateMiddleware] Appended fire sidecar %s",
                    path,
                )
                return
            except OSError:
                continue

    def _is_system_role(self, message: Any) -> bool:
        role = getattr(message, "role", None)
        if role == Role.SYSTEM:
            return True
        if isinstance(role, str) and role.lower() == "system":
            return True
        value = getattr(role, "value", None)
        return isinstance(value, str) and value.lower() == "system"

    def _message_text(self, message: Any) -> str:
        content = getattr(message, "content", None)
        if isinstance(content, str):
            return content
        get_text = getattr(message, "get_text_content", None)
        if callable(get_text):
            return get_text() or ""
        if not content:
            return ""
        parts: list[str] = []
        for block in content:
            text = getattr(block, "text", None)
            if isinstance(text, str):
                parts.append(text)
        return "\n".join(parts)

    def _dump_state(self, state: dict[str, Any]) -> dict[str, Any]:
        return {
            "single_case_events": int(state.get("single_case_events", 0) or 0),
            "generalize_events": int(state.get("generalize_events", 0) or 0),
            "write_events": int(state.get("write_events", 0) or 0),
            "eval_inputs": sorted(
                {str(p) for p in (state.get("eval_inputs") or []) if p}
            ),
            "nudge_count": int(state.get("nudge_count", 0) or 0),
            "last_nudge_iteration": int(
                state.get("last_nudge_iteration", -10_000) or -10_000
            ),
            "nudge_fired": bool(state.get("nudge_fired", False)),
            "last_reason": str(state.get("last_reason") or ""),
            "sticky_reminder": str(state.get("sticky_reminder") or ""),
        }

    def _load_state(self, raw_state: Any) -> dict[str, Any]:
        return self._dump_state(raw_state if isinstance(raw_state, dict) else {})
