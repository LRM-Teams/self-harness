"""Write-on-discovery gate — AHE §6 (Path C / LRM-1016).

Distinct from SHELVED PersistBestCandidate (long-search scorecard), ShortProbe /
StopAfterPass / TimeBudget / Checkpoint / PP, and from KEEP IndependentValidator
/ EnumerateAllAnswers / SanitySmoke / APG. Fires when discovery evidence appears
(flag/answer/solved/computed result) without a subsequent write to the contract
``/app/`` path, or when exploration continues after a writable answer exists.

Inject (Tess TWEAK lesson from Enumerate `2000`): on risk in `after_tool`,
write sidecar + append tool note with literal `WriteOnDiscoveryGate:`
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

STATE_KEY = "write_on_discovery_gate_state"
MARKER = "WriteOnDiscoveryGate:"
logger = logging.getLogger(__name__)

DELIVERABLE_WRITE_RE = re.compile(
    r"(?:"
    r"(?:>>?|tee\b)\s*/app/|"
    r"\b(?:cp|mv|install|rsync)\b[^\n]+/app/|"
    r"cat\s*>\s*/app/|"
    r"\bprintf\b[^\n]*>\s*/app/|"
    r"\bpython3?\s+[^\n]*\bopen\([^)]*['\"]/app/|"
    r"json\.dump\(|write_text\(|Path\([^)]*\)\.write|"
    r"\.(?:write|writelines|dump)\("
    r")",
    re.IGNORECASE,
)

# Evidence the agent found / computed the answer (command or tool output).
DISCOVERY_RE = re.compile(
    r"(?:"
    r"flag\{|"
    r"\b(?:found|discovered|solved|got)\s+(?:the\s+)?(?:answer|flag|solution|result)\b|"
    r"\banswer\s*(?:is|=)\b|"
    r"\bcorrect\s+(?:answer|output|result|move)\b|"
    r"\b(?:final|required)\s+(?:answer|artifact|output)\b|"
    r"\b(?:re\.json|out\.txt|/app/[A-Za-z0-9_.-]+)\b[^\n]{0,40}"
    r"(?:ready|done|complete|written)|"
    r"\b(?:json\.loads|json\.dump)\b[^\n]{0,80}(?:answer|result|keys)|"
    r"\bprint\s*\(\s*(?:answer|result|flag|solution)\b|"
    r"\becho\s+['\"]?(?:answer|flag|solution)\b"
    r")",
    re.IGNORECASE,
)

# Continued optional exploration after discovery (should stop and write).
EXPLORE_RE = re.compile(
    r"(?:"
    r"\bfind\s+/|"
    r"\bls\s+-l[aR]*\s+/|"
    r"\bgrep\s+-R\b|"
    r"\brig\b|\bag\b|"
    r"\b(?:apt|pip|uv)\s+install\b|"
    r"\bwget\b|\bcurl\b[^\n]+http|"
    r"\bpython3?\s+-m\s+pip\b|"
    r"\b(?:sleep|while\s+true)\b"
    r")",
    re.IGNORECASE,
)

_SIDECAR_CANDIDATES = (
    Path("/logs/agent/write_on_discovery.nudge.txt"),
    Path("write_on_discovery.nudge.txt"),
)


class WriteOnDiscoveryGateMiddleware(Middleware):
    """Nudge when discovery evidence lacks immediate contract-path write."""

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
            "discovery_events": 0,
            "write_events": 0,
            "explore_after_discovery": 0,
            "discovery_without_write": 0,
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

        if DELIVERABLE_WRITE_RE.search(command):
            state["write_events"] = int(state["write_events"]) + 1
            state["last_reason"] = "deliverable_write_ok"

        if DISCOVERY_RE.search(blob):
            state["discovery_events"] = int(state["discovery_events"]) + 1
            if int(state["write_events"]) <= 0:
                state["discovery_without_write"] = (
                    int(state["discovery_without_write"]) + 1
                )
                state["last_reason"] = "discovery_without_contract_write"
            else:
                state["last_reason"] = "discovery_after_or_with_write"

        if (
            int(state["discovery_events"]) > 0
            and int(state["write_events"]) <= 0
            and EXPLORE_RE.search(command)
        ):
            state["explore_after_discovery"] = int(state["explore_after_discovery"]) + 1
            state["last_reason"] = "explore_after_discovery_without_write"

        reminder = ""
        tool_output = hook_input.tool_output
        disc_wo = int(state["discovery_without_write"])
        explore = int(state["explore_after_discovery"])
        risk = disc_wo + explore
        if (
            int(state["nudge_count"]) == 0
            and int(state["write_events"]) <= 0
            and risk >= self.min_risk_events
            and (disc_wo > 0 or explore > 0)
        ):
            reason = str(
                state.get("last_reason") or "discovery_without_contract_write"
            )
            reminder = self._build_reminder(
                reason=reason,
                iteration=-1,
                discovery_events=int(state["discovery_events"]),
                write_events=int(state["write_events"]),
                explore_after_discovery=explore,
                discovery_without_write=disc_wo,
            )
            self._write_sidecar(reminder)
            state["nudge_count"] = 1
            state["last_nudge_iteration"] = 0
            state["nudge_fired"] = True
            state["sticky_reminder"] = reminder
            state["message_inject_pending"] = True
            tool_output = self._append_tool_note(tool_output, reminder)
            logger.info(
                "[WriteOnDiscoveryGateMiddleware] after_tool fire #1 reason=%s "
                "disc_wo=%s explore=%s write=%s "
                "(sidecar+tool_note; pending message inject)",
                reason,
                disc_wo,
                explore,
                int(state["write_events"]),
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
                    "[WriteOnDiscoveryGateMiddleware] Sticky SYSTEM/USER re-apply "
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

        disc_wo = int(state["discovery_without_write"])
        explore = int(state["explore_after_discovery"])
        risk = disc_wo + explore
        if risk < self.min_risk_events or int(state["write_events"]) > 0:
            return _passthrough()

        fire = False
        reason = state.get("last_reason") or ""
        if explore > 0:
            fire = True
            reason = reason or "explore_after_discovery_without_write"
        elif disc_wo > 0:
            fire = True
            reason = reason or "discovery_without_contract_write"

        if not fire:
            return _passthrough()

        state["nudge_count"] = int(state["nudge_count"]) + 1
        state["last_nudge_iteration"] = iteration
        state["nudge_fired"] = True
        reminder = self._build_reminder(
            reason=reason,
            iteration=iteration,
            discovery_events=int(state["discovery_events"]),
            write_events=int(state["write_events"]),
            explore_after_discovery=explore,
            discovery_without_write=disc_wo,
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
            "[WriteOnDiscoveryGateMiddleware] Nudge #%s reason=%s iteration=%s "
            "disc_wo=%s explore=%s write=%s system_patched=%s user_patched=%s",
            state["nudge_count"],
            reason,
            iteration,
            disc_wo,
            explore,
            int(state["write_events"]),
            system_patched,
            user_patched,
        )
        return HookResult.with_modifications(messages=updated_messages)

    def _build_reminder(
        self,
        *,
        reason: str,
        iteration: int,
        discovery_events: int,
        write_events: int,
        explore_after_discovery: int,
        discovery_without_write: int,
    ) -> str:
        return (
            f"{MARKER} {reason}; iteration={iteration}; "
            f"discovery_events={discovery_events}; "
            f"write_events={write_events}; "
            f"explore_after_discovery={explore_after_discovery}; "
            f"discovery_without_write={discovery_without_write}. "
            "Manage time explicitly: if you have found the answer/flag or "
            "produced the required artifact, immediately write it to the exact "
            "target path, then run only the smallest evaluator-style verification "
            "needed to confirm it is readable/correct; do not spend the remaining "
            "budget on optional exploration. This is not a request to invent "
            "domain tips or fixture answers."
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
                    "[WriteOnDiscoveryGateMiddleware] Appended fire sidecar %s",
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
            "discovery_events": int(state.get("discovery_events", 0) or 0),
            "write_events": int(state.get("write_events", 0) or 0),
            "explore_after_discovery": int(
                state.get("explore_after_discovery", 0) or 0
            ),
            "discovery_without_write": int(
                state.get("discovery_without_write", 0) or 0
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
