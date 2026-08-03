"""Enumerate-all-answers gate — AHE §4 (Path C / LRM-1014).

Distinct from SHELVED PersistBest / ScratchThenPromote (candidate selection /
promote), ContractFirst / LiteralContract (contract read / token copy),
MirrorEvaluator / StopAfterPass / SemanticNotProxy / GeneralizeNotOverfit.

Fires when the agent writes a deliverable or runs a single happy-path check
without evidence of enumerating / verifying completeness of *all* valid
answers (multi-answer / multi-output families). Process pressure only —
never domain tips or fixture answers.

Tess TWEAK `2000` (LRM-1014): cleaned InMemoryTracer freezes `system_prompt`
from the *first* LLM span, so mid-run SYSTEM PREPEND alone never lands in
cleaned.json. Also, last-write-then-stop never reaches a post-write
`before_model`. Fix: on risk in `after_tool`, write sidecar + append a tool
note carrying literal `EnumerateAllAnswersGate:` (cleaned-durable via
tool_calls.output), set sticky; on next `before_model`, SYSTEM PREPEND/INSERT
+ first-USER PREPEND + sticky SYSTEM-only + FRAMEWORK + trailing USER.
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

STATE_KEY = "enumerate_all_answers_gate_state"
MARKER = "EnumerateAllAnswersGate:"
logger = logging.getLogger(__name__)

# Writing / publishing a deliverable under /app.
DELIVERABLE_WRITE_RE = re.compile(
    r"(?:"
    r"(?:>>?|tee\b)\s*/app/|"
    r"\b(?:cp|mv|install|rsync)\b[^\n]+/app/|"
    r"cat\s*>\s*/app/|"
    r"\bprintf\b[^\n]*>\s*/app/|"
    r"\bpython3?\s+[^\n]*\bopen\([^)]*['\"]/app/|"
    r"json\.dump\(|write_text\(|Path\([^)]*\)\.write"
    r")",
    re.IGNORECASE,
)
# Single-shot / top-1 style validation (one pytest/check, one assert).
SINGLE_CHECK_RE = re.compile(
    r"(?:"
    r"\bpytest\b|"
    r"\bpython3?\s+[^\n]*(?:test_|check_|verify_|validate_)[A-Za-z0-9_]+\.py\b|"
    r"\b(?:assert|diff|cmp)\b|"
    r"\bpython3?\s+-c\s+['\"][^'\"]{0,200}(?:assert|check|verify)"
    r")",
    re.IGNORECASE,
)
# Completeness / enumerate-all evidence (process-level wording or multi-pass).
ENUMERATE_ALL_RE = re.compile(
    r"(?:"
    r"\benumerat|"
    r"\ball\s+(?:valid\s+)?answers?\b|"
    r"\bcompleteness\b|\bcomplete\s+set\b|"
    r"\bevery\s+(?:valid\s+)?(?:answer|solution|move|output)\b|"
    r"\b(?:not|no)\s+(?:just|only)\s+(?:one|top[-_ ]?1|first)\b|"
    r"\bmulti(?:ple)?[-_ ]?(?:answer|output|solution)s?\b|"
    r"\b(?:both|all)\s+(?:mates?|moves?|solutions?|answers?)\b|"
    r"\bfor\s+\w+\s+in\b[^\n]{0,100}(?:answer|solution|move|candidate|case)|"
    r"\bwhile\b[^\n]{0,80}(?:find|collect|enumerat)"
    r")",
    re.IGNORECASE,
)

_SIDECAR_CANDIDATES = (
    Path("/logs/agent/enumerate_all_answers.nudge.txt"),
    Path("enumerate_all_answers.nudge.txt"),
)


class EnumerateAllAnswersGateMiddleware(Middleware):
    """Nudge when deliverable write / check lacks enumerate-all completeness evidence."""

    def __init__(
        self,
        *,
        min_iterations_before_nudge: int = 2,
        renudge_every_iterations: int = 8,
        max_nudges: int = 3,
        min_risk_events: int = 1,
    ) -> None:
        self.min_iterations_before_nudge = int(min_iterations_before_nudge)
        self.renudge_every_iterations = int(renudge_every_iterations)
        self.max_nudges = int(max_nudges)
        self.min_risk_events = int(min_risk_events)

    def before_agent(self, hook_input: BeforeAgentHookInput) -> HookResult:
        state = {
            "write_events": 0,
            "single_check_events": 0,
            "enumerate_events": 0,
            "write_without_enumerate": 0,
            "check_without_enumerate": 0,
            "nudge_count": 0,
            "last_nudge_iteration": -10_000,
            "nudge_fired": False,
            "last_reason": "",
            "sticky_reminder": "",
            "message_inject_pending": False,
        }
        hook_input.agent_state.set_global_value(STATE_KEY, self._dump_state(state))
        return HookResult.no_changes()

    def after_tool(self, hook_input: AfterToolHookInput) -> HookResult:
        if hook_input.tool_name != "run_shell_command":
            return HookResult.no_changes()

        command = self._extract_command(hook_input.tool_input)
        state = self._load_state(hook_input.agent_state.get_global_value(STATE_KEY, {}))

        if ENUMERATE_ALL_RE.search(command):
            state["enumerate_events"] = int(state["enumerate_events"]) + 1
            state["last_reason"] = "enumerate_all_ok"

        if DELIVERABLE_WRITE_RE.search(command):
            state["write_events"] = int(state["write_events"]) + 1
            if int(state["enumerate_events"]) <= 0:
                state["write_without_enumerate"] = (
                    int(state["write_without_enumerate"]) + 1
                )
                state["last_reason"] = "deliverable_write_without_enumerate_all"
            else:
                state["last_reason"] = "deliverable_write_after_enumerate"

        if SINGLE_CHECK_RE.search(command):
            state["single_check_events"] = int(state["single_check_events"]) + 1
            if int(state["enumerate_events"]) <= 0:
                state["check_without_enumerate"] = (
                    int(state["check_without_enumerate"]) + 1
                )
                state["last_reason"] = "single_check_without_enumerate_all"
            else:
                state["last_reason"] = "check_with_prior_enumerate"

        # Tess TWEAK `2000`: first fire at risk time so last-write-then-stop still
        # lands sidecar + cleaned tool-note marker (no post-write before_model).
        # Renudges stay on before_model (has iteration clock).
        reminder = ""
        tool_output = hook_input.tool_output
        write_wo = int(state["write_without_enumerate"])
        check_wo = int(state["check_without_enumerate"])
        if (
            int(state["nudge_count"]) == 0
            and int(state["enumerate_events"]) <= 0
            and (write_wo + check_wo) >= self.min_risk_events
            and (write_wo > 0 or check_wo > 0)
        ):
            reason = str(state.get("last_reason") or "deliverable_write_without_enumerate_all")
            reminder = self._build_reminder(
                reason=reason,
                iteration=-1,
                write_events=int(state["write_events"]),
                single_check_events=int(state["single_check_events"]),
                enumerate_events=int(state["enumerate_events"]),
                write_wo=write_wo,
                check_wo=check_wo,
            )
            self._write_sidecar(reminder)
            state["nudge_count"] = 1
            state["last_nudge_iteration"] = 0
            state["nudge_fired"] = True
            state["sticky_reminder"] = reminder
            state["message_inject_pending"] = True
            tool_output = self._append_tool_note(tool_output, reminder)
            logger.info(
                "[EnumerateAllAnswersGateMiddleware] after_tool fire #1 reason=%s "
                "write_wo=%s check_wo=%s enum=%s (sidecar+tool_note; pending message inject)",
                reason,
                write_wo,
                check_wo,
                int(state["enumerate_events"]),
            )

        hook_input.agent_state.set_global_value(STATE_KEY, self._dump_state(state))
        if reminder:
            return HookResult.with_modifications(tool_output=tool_output)
        return HookResult.no_changes()

    def before_model(self, hook_input: BeforeModelHookInput) -> HookResult:
        iteration = int(hook_input.current_iteration or 0)
        state = self._load_state(hook_input.agent_state.get_global_value(STATE_KEY, {}))
        sticky = str(state.get("sticky_reminder") or "")
        messages = list(hook_input.messages)
        sticky_applied = False
        pending_inject = bool(state.get("message_inject_pending"))

        # Tess TWEAK: sticky 只认 SYSTEM — cleaned freezes first system_prompt;
        # still re-paint SYSTEM for the live model + first-USER when pending.
        if sticky and (
            pending_inject
            or not self._system_has_marker(messages, MARKER)
            or not self._user_has_marker(messages, MARKER)
        ):
            messages, sys_ok = self._apply_system_marker(messages, sticky)
            messages, user_ok = self._apply_user_marker(messages, sticky)
            sticky_applied = sys_ok or user_ok
            if sticky_applied:
                logger.info(
                    "[EnumerateAllAnswersGateMiddleware] Sticky SYSTEM/USER re-apply "
                    "iteration=%s system=%s user=%s pending=%s",
                    iteration,
                    sys_ok,
                    user_ok,
                    pending_inject,
                )

        def _passthrough() -> HookResult:
            if pending_inject and sticky:
                # Finish Tess inject surfaces even when fire already happened in after_tool.
                updated = list(messages)
                updated.append(
                    Message(role=Role.FRAMEWORK, content=[TextBlock(text=sticky)])
                )
                updated.append(
                    Message(role=Role.USER, content=[TextBlock(text=sticky)])
                )
                state["message_inject_pending"] = False
                state["last_nudge_iteration"] = iteration
                hook_input.agent_state.set_global_value(STATE_KEY, self._dump_state(state))
                return HookResult.with_modifications(messages=updated)
            if sticky_applied:
                hook_input.agent_state.set_global_value(STATE_KEY, self._dump_state(state))
                return HookResult.with_modifications(messages=messages)
            return HookResult.no_changes()

        if iteration < self.min_iterations_before_nudge:
            return _passthrough()

        has_assistant = any(
            getattr(message, "role", None) == Role.ASSISTANT for message in messages
        )
        if not has_assistant:
            return _passthrough()

        # after_tool already recorded fire + sticky; finish message surfaces once.
        if pending_inject:
            return _passthrough()

        # Renudge path only (first fire is after_tool).
        if state["nudge_count"] >= self.max_nudges:
            return _passthrough()
        if (
            state["nudge_count"] > 0
            and iteration - state["last_nudge_iteration"] < self.renudge_every_iterations
        ):
            return _passthrough()

        write_wo = int(state["write_without_enumerate"])
        check_wo = int(state["check_without_enumerate"])
        risk = write_wo + check_wo
        if risk < self.min_risk_events:
            return _passthrough()

        fire = False
        reason = state.get("last_reason") or ""
        if write_wo > 0 and int(state["enumerate_events"]) <= 0:
            fire = True
            reason = reason or "deliverable_write_without_enumerate_all"
        elif check_wo > 0 and int(state["enumerate_events"]) <= 0:
            fire = True
            reason = reason or "single_check_without_enumerate_all"

        if not fire:
            return _passthrough()

        # Fresh before_model fire / renudge (after_tool already took first fire).
        state["nudge_count"] = int(state["nudge_count"]) + 1
        state["last_nudge_iteration"] = iteration
        state["nudge_fired"] = True
        reminder = self._build_reminder(
            reason=reason,
            iteration=iteration,
            write_events=int(state["write_events"]),
            single_check_events=int(state["single_check_events"]),
            enumerate_events=int(state["enumerate_events"]),
            write_wo=write_wo,
            check_wo=check_wo,
        )
        self._write_sidecar(reminder)
        state["sticky_reminder"] = reminder
        state["message_inject_pending"] = False
        hook_input.agent_state.set_global_value(STATE_KEY, self._dump_state(state))

        # Tess GO/TWEAK: SYSTEM PREPEND (else INSERT) + first-USER PREPEND +
        # sticky SYSTEM-only + FRAMEWORK + trailing USER + sidecar.
        updated_messages, system_patched = self._apply_system_marker(messages, reminder)
        updated_messages, user_patched = self._apply_user_marker(
            updated_messages, reminder
        )
        updated_messages.append(
            Message(role=Role.FRAMEWORK, content=[TextBlock(text=reminder)])
        )
        updated_messages.append(
            Message(role=Role.USER, content=[TextBlock(text=reminder)])
        )
        logger.info(
            "[EnumerateAllAnswersGateMiddleware] Nudge #%s reason=%s iteration=%s "
            "write_wo=%s check_wo=%s enum=%s system_patched=%s user_patched=%s",
            state["nudge_count"],
            reason,
            iteration,
            write_wo,
            check_wo,
            int(state["enumerate_events"]),
            system_patched,
            user_patched,
        )
        return HookResult.with_modifications(messages=updated_messages)

    def _build_reminder(
        self,
        *,
        reason: str,
        iteration: int,
        write_events: int,
        single_check_events: int,
        enumerate_events: int,
        write_wo: int,
        check_wo: int,
    ) -> str:
        return (
            f"{MARKER} {reason}; iteration={iteration}; "
            f"write_events={write_events}; "
            f"single_check_events={single_check_events}; "
            f"enumerate_events={enumerate_events}; "
            f"write_without_enumerate={write_wo}; "
            f"check_without_enumerate={check_wo}. "
            "Control candidate selection: if the task may require all valid "
            "answers rather than one top-1 answer, enumerate and verify "
            "completeness before finishing. Do not submit a partial set. "
            "This is not a request to invent domain tips or fixture answers."
        )

    def _extract_command(self, tool_input: Any) -> str:
        if not isinstance(tool_input, dict):
            return ""
        direct = tool_input.get("command")
        if isinstance(direct, str) and direct.strip():
            return direct
        params = tool_input.get("parameters")
        if isinstance(params, dict):
            nested = params.get("command")
            if isinstance(nested, str) and nested.strip():
                return nested
        return ""

    def _append_tool_note(self, tool_output: Any, reminder: str) -> Any:
        note = reminder if reminder.startswith(MARKER) else f"{MARKER} {reminder}"
        line = f"Harness note: {note}"
        if isinstance(tool_output, dict):
            updated = dict(tool_output)
            existing = ""
            content = updated.get("content")
            if isinstance(content, str):
                existing = content
            elif content is not None:
                existing = str(content)
            if MARKER in existing:
                return updated
            updated["content"] = existing + ("\n" if existing else "") + line
            return updated
        existing = tool_output if isinstance(tool_output, str) else str(tool_output or "")
        if MARKER in existing:
            return tool_output
        return existing + ("\n" if existing else "") + line

    def _system_has_marker(self, messages: list[Message], marker: str) -> bool:
        for message in messages:
            if self._is_system_role(message) and marker in self._message_text(message):
                return True
        return False

    def _user_has_marker(self, messages: list[Message], marker: str) -> bool:
        for message in messages:
            if getattr(message, "role", None) == Role.USER and marker in self._message_text(
                message
            ):
                return True
        return False

    def _apply_system_marker(
        self, messages: list[Message], reminder: str
    ) -> tuple[list[Message], bool]:
        updated_messages: list[Message] = []
        system_patched = False
        for message in messages:
            if not system_patched and (
                getattr(message, "role", None) == Role.SYSTEM
                or self._is_system_role(message)
            ):
                existing = ""
                get_text = getattr(message, "get_text_content", None)
                if callable(get_text):
                    existing = get_text() or ""
                else:
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
                existing = ""
                get_text = getattr(message, "get_text_content", None)
                if callable(get_text):
                    existing = get_text() or ""
                else:
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
                    "[EnumerateAllAnswersGateMiddleware] Appended fire sidecar %s",
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
            "write_events": int(state.get("write_events", 0) or 0),
            "single_check_events": int(state.get("single_check_events", 0) or 0),
            "enumerate_events": int(state.get("enumerate_events", 0) or 0),
            "write_without_enumerate": int(
                state.get("write_without_enumerate", 0) or 0
            ),
            "check_without_enumerate": int(
                state.get("check_without_enumerate", 0) or 0
            ),
            "nudge_count": int(state.get("nudge_count", 0) or 0),
            "last_nudge_iteration": int(
                state.get("last_nudge_iteration", -10_000) or -10_000
            ),
            "nudge_fired": bool(state.get("nudge_fired", False)),
            "last_reason": str(state.get("last_reason") or ""),
            "sticky_reminder": str(state.get("sticky_reminder") or ""),
            "message_inject_pending": bool(state.get("message_inject_pending", False)),
        }

    def _load_state(self, raw: Any) -> dict[str, Any]:
        if not isinstance(raw, dict):
            raw = {}
        return self._dump_state(raw)
