"""Forbidden-extras gate — AHE §2 filesystem-constrained (Path C / LRM-1019).

Distinct from SHELVED MirrorEvaluator / ExactSourceLayout / RereadDeliverable /
EndStateReady / CanonicalEntry / SemanticNotProxy / PreserveSemantics / §6
timeout-publish family, and from KEEP IndependentValidator / Enumerate /
SanitySmoke / APG. Fires when a constrained deliverable directory shows
compile/run success with leftover binaries/temp/cache/extra files, or when no
extras/pollution sweep evidence appears before finish.

Inject (Tess TWEAK lesson from Enumerate `2000`): on risk in `after_tool`,
write sidecar + append tool note with literal `ForbiddenExtrasGate:`
(cleaned-durable); on next `before_model`, SYSTEM PREPEND/INSERT + first-USER
PREPEND + sticky SYSTEM-only + FRAMEWORK + trailing USER.
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

STATE_KEY = "forbidden_extras_gate_state"
MARKER = "ForbiddenExtrasGate:"
logger = logging.getLogger(__name__)

# Compile / link that commonly leaves leftover binaries beside the deliverable.
COMPILE_RE = re.compile(
    r"(?:"
    r"\b(?:gcc|g\+\+|clang\+\+|clang|rustc|cc)\b|"
    r"\b(?:cargo|make|cmake|ninja)\b|"
    r"-o\s+(?:cmain|main|a\.out|/app/[A-Za-z0-9_.-]+)"
    r")",
    re.IGNORECASE,
)

# Leftover / pollution evidence in command or tool output.
POLLUTION_RE = re.compile(
    r"(?:"
    r"\bcmain\b|\ba\.out\b|"
    r"(?:^|\s)main(?:\s|$)|"
    r"\b__pycache__\b|\b\.o\b|\b\.obj\b|\b\.dSYM\b|"
    r"\bfound:\s*\[|"
    r"Expected only\b|"
    r"leftover|extra (?:file|binary|binaries)|directory pollution"
    r")",
    re.IGNORECASE,
)

# Evidence of an extras / pollution sweep.
SWEEP_RE = re.compile(
    r"(?:"
    r"\brm\s+(?:-f\s+)?(?:cmain|main|a\.out)\b|"
    r"\brm\s+-rf\s+(?:__pycache__|\*\.o)\b|"
    r"\bls\s+/app\b|"
    r"\bfind\s+/app\b|"
    r"\b(?:only|exactly)\s+(?:one\s+)?(?:file|deliverable)\b|"
    r"\bno\s+(?:extra|leftover)\b|"
    r"\bpollution\b|\bforbidden\s+extras?\b|"
    r"\bclean(?:up)?\s+(?:extras?|binaries|temp)\b"
    r")",
    re.IGNORECASE,
)

_SIDECAR_CANDIDATES = (
    Path("/logs/agent/forbidden_extras.nudge.txt"),
    Path("forbidden_extras.nudge.txt"),
)


class ForbiddenExtrasGateMiddleware(Middleware):
    """Nudge when constrained dirs keep leftover binaries/temp without a sweep."""

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
            "compile_events": 0,
            "pollution_events": 0,
            "sweep_events": 0,
            "compile_without_sweep": 0,
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
        output_text = self._tool_output_text(hook_input.tool_output)
        blob = f"{command}\n{output_text}"
        state = self._load_state(hook_input.agent_state.get_global_value(STATE_KEY, {}))

        if SWEEP_RE.search(blob):
            state["sweep_events"] = int(state["sweep_events"]) + 1
            state["last_reason"] = "extras_sweep_ok"

        if COMPILE_RE.search(command):
            state["compile_events"] = int(state["compile_events"]) + 1
            if int(state["sweep_events"]) <= 0:
                state["compile_without_sweep"] = (
                    int(state["compile_without_sweep"]) + 1
                )
                state["last_reason"] = "compile_without_extras_sweep"
            else:
                state["last_reason"] = "compile_with_prior_or_later_sweep"

        if POLLUTION_RE.search(blob):
            state["pollution_events"] = int(state["pollution_events"]) + 1
            if int(state["sweep_events"]) <= 0:
                state["last_reason"] = "pollution_without_extras_sweep"

        reminder = ""
        tool_output = hook_input.tool_output
        compile_wo = int(state["compile_without_sweep"])
        pollution = int(state["pollution_events"])
        risk = compile_wo + (1 if pollution > 0 and int(state["sweep_events"]) <= 0 else 0)
        if (
            int(state["nudge_count"]) == 0
            and int(state["sweep_events"]) <= 0
            and risk >= self.min_risk_events
            and (compile_wo > 0 or pollution > 0)
        ):
            reason = str(
                state.get("last_reason") or "compile_without_extras_sweep"
            )
            reminder = self._build_reminder(
                reason=reason,
                iteration=-1,
                compile_events=int(state["compile_events"]),
                pollution_events=pollution,
                sweep_events=int(state["sweep_events"]),
                compile_without_sweep=compile_wo,
            )
            self._write_sidecar(reminder)
            state["nudge_count"] = 1
            state["last_nudge_iteration"] = 0
            state["nudge_fired"] = True
            state["sticky_reminder"] = reminder
            state["message_inject_pending"] = True
            tool_output = self._append_tool_note(tool_output, reminder)
            logger.info(
                "[ForbiddenExtrasGateMiddleware] after_tool fire #1 reason=%s "
                "compile_wo=%s pollution=%s sweep=%s "
                "(sidecar+tool_note; pending message inject)",
                reason,
                compile_wo,
                pollution,
                int(state["sweep_events"]),
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
                    "[ForbiddenExtrasGateMiddleware] Sticky SYSTEM/USER re-apply "
                    "iteration=%s system=%s user=%s pending=%s",
                    iteration,
                    sys_ok,
                    user_ok,
                    pending_inject,
                )

        def _passthrough() -> HookResult:
            if pending_inject and sticky:
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

        if pending_inject:
            return _passthrough()

        if state["nudge_count"] >= self.max_nudges:
            return _passthrough()
        if (
            state["nudge_count"] > 0
            and iteration - state["last_nudge_iteration"] < self.renudge_every_iterations
        ):
            return _passthrough()

        compile_wo = int(state["compile_without_sweep"])
        pollution = int(state["pollution_events"])
        sweep = int(state["sweep_events"])
        if sweep > 0:
            return _passthrough()
        risk = compile_wo + (1 if pollution > 0 else 0)
        if risk < self.min_risk_events:
            return _passthrough()

        fire = False
        reason = state.get("last_reason") or ""
        if pollution > 0:
            fire = True
            reason = reason or "pollution_without_extras_sweep"
        elif compile_wo > 0:
            fire = True
            reason = reason or "compile_without_extras_sweep"

        if not fire:
            return _passthrough()

        state["nudge_count"] = int(state["nudge_count"]) + 1
        state["last_nudge_iteration"] = iteration
        state["nudge_fired"] = True
        reminder = self._build_reminder(
            reason=reason,
            iteration=iteration,
            compile_events=int(state["compile_events"]),
            pollution_events=pollution,
            sweep_events=sweep,
            compile_without_sweep=compile_wo,
        )
        self._write_sidecar(reminder)
        state["sticky_reminder"] = reminder
        state["message_inject_pending"] = False
        hook_input.agent_state.set_global_value(STATE_KEY, self._dump_state(state))

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
            "[ForbiddenExtrasGateMiddleware] Nudge #%s reason=%s iteration=%s "
            "compile_wo=%s pollution=%s sweep=%s system_patched=%s user_patched=%s",
            state["nudge_count"],
            reason,
            iteration,
            compile_wo,
            pollution,
            sweep,
            system_patched,
            user_patched,
        )
        return HookResult.with_modifications(messages=updated_messages)

    def _build_reminder(
        self,
        *,
        reason: str,
        iteration: int,
        compile_events: int,
        pollution_events: int,
        sweep_events: int,
        compile_without_sweep: int,
    ) -> str:
        return (
            f"{MARKER} {reason}; iteration={iteration}; "
            f"compile_events={compile_events}; "
            f"pollution_events={pollution_events}; "
            f"sweep_events={sweep_events}; "
            f"compile_without_sweep={compile_without_sweep}. "
            "For filesystem-constrained tasks, verify both required files and "
            "absence of leftover binaries, temp files, cache directories, or other "
            "directory pollution before finishing. Do not rely on "
            "`find ... -type f` alone when extra directories would fail the contract. "
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

    def _tool_output_text(self, tool_output: Any) -> str:
        if isinstance(tool_output, dict):
            content = tool_output.get("content")
            if isinstance(content, str):
                return content
            if content is not None:
                return str(content)
            return str(tool_output)
        if isinstance(tool_output, str):
            return tool_output
        return str(tool_output or "")

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
                    "[ForbiddenExtrasGateMiddleware] Appended fire sidecar %s",
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
            "compile_events": int(state.get("compile_events", 0) or 0),
            "pollution_events": int(state.get("pollution_events", 0) or 0),
            "sweep_events": int(state.get("sweep_events", 0) or 0),
            "compile_without_sweep": int(state.get("compile_without_sweep", 0) or 0),
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
