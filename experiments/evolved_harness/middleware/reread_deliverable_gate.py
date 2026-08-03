"""Reread-deliverable gate — AHE §2 (Path C / LRM-973).

Distinct from KEEP ArtifactPublishGuard (missing publish path), SHELVED
EndStateReady (environment end state), OfficialChecker, and LiteralContract.
Fires when a contract deliverable path was written/generated but not re-read
from disk (or re-invoked at the public entry) before the agent prepares to stop.
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

STATE_KEY = "reread_deliverable_gate_state"
logger = logging.getLogger(__name__)

DELIVERABLE_PATH_RE = re.compile(r"(/app/[A-Za-z0-9_./-]+)")
REQUIRED_HINT_RE = re.compile(
    r"(?:stored in|write(?:n)? to|save(?:d)? (?:to|as)|output(?: file)?|"
    r"file titled|create(?:s|d)?|produce(?:s|d)?)"
    r"[^\n]{0,40}?(/app/[A-Za-z0-9_./-]+)",
    re.IGNORECASE,
)
WRITE_CMD_RE = re.compile(
    r"(?:"
    r"(?:>>?|tee\b|cat\s*>)\s*[^\n]*(/app/[A-Za-z0-9_./-]+)|"
    r"(?:cp|mv|install)\b[^\n]+(/app/[A-Za-z0-9_./-]+)|"
    r"write_text\s*\(\s*['\"](/app/[A-Za-z0-9_./-]+)['\"]|"
    r"open\s*\(\s*['\"](/app/[A-Za-z0-9_./-]+)['\"][^)]*['\"]w|"
    r"(?:\.save|json\.dump|pickle\.dump|to_csv|to_json)\s*\([^\n]*"
    r"(/app/[A-Za-z0-9_./-]+)"
    r")",
    re.IGNORECASE,
)
REREAD_CMD_RE = re.compile(
    r"(?:"
    r"\b(?:cat|head|tail|less|more|sha256sum|md5sum|wc|stat|xxd|hexdump|"
    r"file|strings|od)\b[^\n]*(/app/[A-Za-z0-9_./-]+)|"
    r"\bpython3?\b[^\n]*(?:open|Path|read_text|readlines)\s*\([^\n]*"
    r"(/app/[A-Za-z0-9_./-]+)|"
    r"\b(?:pytest|python3?)\b[^\n]*(/app/[A-Za-z0-9_./-]+)"
    r")",
    re.IGNORECASE,
)

_SIDECAR_CANDIDATES = (
    Path("/logs/agent/reread_deliverable.nudge.txt"),
    Path("reread_deliverable.nudge.txt"),
)


class RereadDeliverableGateMiddleware(Middleware):
    """Nudge when deliverables were written but not re-read from disk."""

    def __init__(
        self,
        *,
        min_iterations_before_nudge: int = 3,
        renudge_every_iterations: int = 8,
        max_nudges: int = 3,
        min_write_events: int = 1,
    ) -> None:
        self.min_iterations_before_nudge = int(min_iterations_before_nudge)
        self.renudge_every_iterations = int(renudge_every_iterations)
        self.max_nudges = int(max_nudges)
        self.min_write_events = int(min_write_events)

    def before_agent(self, hook_input: BeforeAgentHookInput) -> HookResult:
        required: set[str] = set()
        for message in hook_input.messages:
            if getattr(message, "role", None) != Role.USER:
                continue
            text = self._message_text(message)
            for path in DELIVERABLE_PATH_RE.findall(text):
                required.add(path.rstrip(".,;)'\""))
            for match in REQUIRED_HINT_RE.finditer(text):
                required.add(match.group(1).rstrip(".,;)'\""))

        state = {
            "required_paths": sorted(required),
            "written_paths": [],
            "reread_paths": [],
            "write_events": 0,
            "reread_events": 0,
            "nudge_count": 0,
            "last_nudge_iteration": -10_000,
            "nudge_fired": False,
            "last_reason": "",
            "sticky_reminder": "",
        }
        hook_input.agent_state.set_global_value(STATE_KEY, self._dump_state(state))
        if required:
            logger.info(
                "[RereadDeliverableGateMiddleware] Required paths from task: %s",
                sorted(required),
            )
        return HookResult.no_changes()

    def after_tool(self, hook_input: AfterToolHookInput) -> HookResult:
        if hook_input.tool_name != "run_shell_command":
            return HookResult.no_changes()

        tool_input = hook_input.tool_input if isinstance(hook_input.tool_input, dict) else {}
        command = str(tool_input.get("command", ""))
        state = self._load_state(hook_input.agent_state.get_global_value(STATE_KEY, {}))
        written = set(state["written_paths"])
        reread = set(state["reread_paths"])
        required = set(state["required_paths"])

        for match in WRITE_CMD_RE.finditer(command):
            path = next((g for g in match.groups() if g), None)
            if not path:
                continue
            path = path.rstrip(".,;)'\"")
            written.add(path)
            state["write_events"] = int(state["write_events"]) + 1
            state["last_reason"] = f"deliverable_write:{path}"

        for match in REREAD_CMD_RE.finditer(command):
            path = next((g for g in match.groups() if g), None)
            if not path:
                continue
            path = path.rstrip(".,;)'\"")
            reread.add(path)
            state["reread_events"] = int(state["reread_events"]) + 1
            state["last_reason"] = f"deliverable_reread:{path}"

        # Also count bare /app paths mentioned with write-ish operators nearby.
        if WRITE_CMD_RE.search(command) is None and re.search(
            r"(?:>>?|tee\b|write_text|open\([^)]*['\"]w)", command, re.IGNORECASE
        ):
            for path in DELIVERABLE_PATH_RE.findall(command):
                path = path.rstrip(".,;)'\"")
                if path in required or path.startswith("/app/"):
                    written.add(path)
                    state["write_events"] = int(state["write_events"]) + 1
                    state["last_reason"] = f"deliverable_write:{path}"

        state["written_paths"] = sorted(written)
        state["reread_paths"] = sorted(reread)
        hook_input.agent_state.set_global_value(STATE_KEY, self._dump_state(state))
        return HookResult.no_changes()

    def before_model(self, hook_input: BeforeModelHookInput) -> HookResult:
        iteration = int(hook_input.current_iteration or 0)
        state = self._load_state(hook_input.agent_state.get_global_value(STATE_KEY, {}))
        sticky = str(state.get("sticky_reminder") or "")
        messages = list(hook_input.messages)
        sticky_applied = False

        if sticky and not self._messages_have_marker(messages, "RereadDeliverableGate:"):
            messages, sticky_applied = self._apply_system_marker(messages, sticky)
            if sticky_applied:
                logger.info(
                    "[RereadDeliverableGateMiddleware] Sticky SYSTEM re-apply "
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

        if int(state["write_events"]) < self.min_write_events:
            return _passthrough()

        written = set(state["written_paths"])
        reread = set(state["reread_paths"])
        required = set(state["required_paths"])
        focus = written & required if (written & required) else written
        unread = sorted(focus - reread)
        if not unread:
            return _passthrough()

        reason = state.get("last_reason") or "write_without_reread"
        state["nudge_count"] += 1
        state["last_nudge_iteration"] = iteration
        state["nudge_fired"] = True

        sample = ", ".join(unread[:4])
        reminder = (
            f"RereadDeliverableGate: {reason}; iteration={iteration}; "
            f"write_events={state['write_events']}; "
            f"reread_events={state['reread_events']}; "
            f"unread={sample}. "
            "After the final write, reread the exact deliverable from disk "
            "(cat/head/sha256sum/python open) or call the exact public entry "
            "with evaluator-style arguments. Do not invent domain tips from "
            "this reminder."
        )
        self._write_sidecar(reminder)
        state["sticky_reminder"] = reminder
        hook_input.agent_state.set_global_value(STATE_KEY, self._dump_state(state))

        updated_messages, system_patched = self._apply_system_marker(messages, reminder)
        updated_messages.append(Message(role=Role.USER, content=[TextBlock(text=reminder)]))
        logger.info(
            "[RereadDeliverableGateMiddleware] Nudge #%s reason=%s iteration=%s "
            "writes=%s rereads=%s unread=%s system_patched=%s",
            state["nudge_count"],
            reason,
            iteration,
            state["write_events"],
            state["reread_events"],
            unread,
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
                    "[RereadDeliverableGateMiddleware] Appended fire sidecar %s", path
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
            "required_paths": sorted(
                {str(p) for p in (state.get("required_paths") or []) if p}
            ),
            "written_paths": sorted(
                {str(p) for p in (state.get("written_paths") or []) if p}
            ),
            "reread_paths": sorted(
                {str(p) for p in (state.get("reread_paths") or []) if p}
            ),
            "write_events": int(state.get("write_events", 0) or 0),
            "reread_events": int(state.get("reread_events", 0) or 0),
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
