"""End-state-ready gate — AHE §7 (Path C / LRM-970).

Distinct from SHELVED StopAfterPass (stop polish after eval pass),
PreCompletion (checklist block-exit), and CanonicalEntry (re-run public entry).
Fires when the agent has written scripts/helpers/docs or half-finished work but
has not yet brought the environment itself into the evaluator-ready end state.
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

STATE_KEY = "end_state_ready_gate_state"
logger = logging.getLogger(__name__)

# Wrote a helper/script/doc instead of (or before) applying end state.
SCRIPT_OR_DOC_WRITE_RE = re.compile(
    r"(?:"
    r"(?:>>?|tee\b|cat\s*>|write_text|open\([^)]*['\"]w)[^\n]*\.(?:sh|py|md|txt|bat|ps1)\b|"
    r"\bprintf\b[^\n]*>\s*[^\n]+\.(?:sh|py|md)\b"
    r")",
    re.IGNORECASE,
)

# Evidence the environment/end state was actually applied/materialized.
APPLY_END_STATE_RE = re.compile(
    r"(?:"
    r"\bbash\s+[^\n]+\.sh\b|\bsh\s+[^\n]+\.sh\b|\bsource\s+[^\n]+\.sh\b|"
    r"\bpython3?\s+[^\n]+\.py\b|"
    r"\bsystemctl\b|\bservice\b|\bnohup\b|\bdocker\s+run\b|"
    r"\bapt(?:-get)?\s+install\b|\bpip3?\s+install\b|\bchoco\b|\bwinget\b|"
    r"\bwmic\b|\bpowershell\b|\breg\s+add\b|"
    r"\bsqlite3\b|\bprisma\b|\balembic\b|"
    r"(?:cp|mv|install)\b[^\n]*/app/|"
    r"\bmkdir\s+-p\s+/app\b"
    r")",
    re.IGNORECASE,
)

# Explicit defer / "user can run later" process smell in the command itself.
DEFER_RE = re.compile(
    r"(?:"
    r"run\s+this\s+later|user\s+can\s+run|TODO:?\s*run|"
    r"chmod\s+\+x\b[^\n]*&&\s*echo\b|"
    r"echo\s+['\"][^'\"]*(?:run|execute|install)[^'\"]*['\"]"
    r")",
    re.IGNORECASE,
)

_SIDECAR_CANDIDATES = (
    Path("/logs/agent/end_state_ready.nudge.txt"),
    Path("end_state_ready.nudge.txt"),
)


class EndStateReadyGateMiddleware(Middleware):
    """Nudge when scripts/half-work exist but the end state is not ready now."""

    def __init__(
        self,
        *,
        min_iterations_before_nudge: int = 3,
        renudge_every_iterations: int = 8,
        max_nudges: int = 3,
        min_script_events: int = 1,
    ) -> None:
        self.min_iterations_before_nudge = int(min_iterations_before_nudge)
        self.renudge_every_iterations = int(renudge_every_iterations)
        self.max_nudges = int(max_nudges)
        self.min_script_events = int(min_script_events)

    def before_agent(self, hook_input: BeforeAgentHookInput) -> HookResult:
        state = {
            "script_events": 0,
            "apply_events": 0,
            "defer_events": 0,
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

        if SCRIPT_OR_DOC_WRITE_RE.search(command):
            state["script_events"] = int(state["script_events"]) + 1
            state["last_reason"] = "script_or_doc_write"

        if DEFER_RE.search(command):
            state["defer_events"] = int(state["defer_events"]) + 1
            state["last_reason"] = "defer_run_later"

        if APPLY_END_STATE_RE.search(command):
            state["apply_events"] = int(state["apply_events"]) + 1
            state["last_reason"] = "end_state_apply_seen"

        hook_input.agent_state.set_global_value(STATE_KEY, self._dump_state(state))
        return HookResult.no_changes()

    def before_model(self, hook_input: BeforeModelHookInput) -> HookResult:
        iteration = int(hook_input.current_iteration or 0)
        state = self._load_state(hook_input.agent_state.get_global_value(STATE_KEY, {}))
        sticky = str(state.get("sticky_reminder") or "")
        messages = list(hook_input.messages)
        sticky_applied = False

        # Align LiteralContract/`2004` sticky: keep marker on SYSTEM surface.
        if sticky and not self._messages_have_marker(messages, "EndStateReadyGate:"):
            messages, sticky_applied = self._apply_system_marker(messages, sticky)
            if sticky_applied:
                logger.info(
                    "[EndStateReadyGateMiddleware] Sticky SYSTEM re-apply "
                    "iteration=%s",
                    iteration,
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

        # Fire when helper/script/doc work exists (or defer smell) without
        # evidence the environment end state was applied.
        script_ok = int(state["script_events"]) >= self.min_script_events
        defer_ok = int(state["defer_events"]) >= 1
        if not (script_ok or defer_ok):
            return _passthrough()
        if int(state["apply_events"]) >= 1 and int(state["defer_events"]) == 0:
            return _passthrough()

        reason = state.get("last_reason") or "script_without_end_state"
        state["nudge_count"] += 1
        state["last_nudge_iteration"] = iteration
        state["nudge_fired"] = True

        reminder = (
            f"EndStateReadyGate: {reason}; iteration={iteration}; "
            f"script_events={state['script_events']}; "
            f"apply_events={state['apply_events']}; "
            f"defer_events={state['defer_events']}. "
            "Do not stop at writing a script or telling a future user how to "
            "run it. Bring the environment itself into the evaluator-ready "
            "final state now, then do a last acceptance sweep. Do not invent "
            "domain tips from this reminder."
        )
        self._write_sidecar(reminder)
        state["sticky_reminder"] = reminder
        hook_input.agent_state.set_global_value(STATE_KEY, self._dump_state(state))

        updated_messages, system_patched = self._apply_system_marker(messages, reminder)
        updated_messages.append(Message(role=Role.USER, content=[TextBlock(text=reminder)]))
        logger.info(
            "[EndStateReadyGateMiddleware] Nudge #%s reason=%s iteration=%s "
            "scripts=%s apply=%s defer=%s system_patched=%s",
            state["nudge_count"],
            reason,
            iteration,
            state["script_events"],
            state["apply_events"],
            state["defer_events"],
            system_patched,
        )
        return HookResult.with_modifications(messages=updated_messages)

    def _messages_have_marker(self, messages: list[Message], marker: str) -> bool:
        for message in messages:
            if self._is_system_role(message) and marker in self._message_text(message):
                return True
        return False

    def _apply_system_marker(
        self, messages: list[Message], reminder: str
    ) -> tuple[list[Message], bool]:
        """PREPEND into first SYSTEM if present; else INSERT SYSTEM at front."""
        updated_messages: list[Message] = []
        system_patched = False
        for message in messages:
            if not system_patched and self._is_system_role(message):
                existing = self._message_text(message)
                if reminder not in existing:
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

    def _write_sidecar(self, text: str) -> None:
        for path in _SIDECAR_CANDIDATES:
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                with path.open("a", encoding="utf-8") as fh:
                    fh.write(text.rstrip() + "\n")
                logger.info(
                    "[EndStateReadyGateMiddleware] Appended fire sidecar %s", path
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
            "script_events": int(state.get("script_events", 0) or 0),
            "apply_events": int(state.get("apply_events", 0) or 0),
            "defer_events": int(state.get("defer_events", 0) or 0),
            "nudge_count": int(state.get("nudge_count", 0) or 0),
            "last_nudge_iteration": int(state.get("last_nudge_iteration", -10_000) or -10_000),
            "nudge_fired": bool(state.get("nudge_fired", False)),
            "last_reason": str(state.get("last_reason") or ""),
            "sticky_reminder": str(state.get("sticky_reminder") or ""),
        }

    def _load_state(self, raw_state: Any) -> dict[str, Any]:
        return self._dump_state(raw_state if isinstance(raw_state, dict) else {})
