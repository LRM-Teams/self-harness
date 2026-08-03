"""Boundary-case check gate — AHE §5 limits/cancellation/ordering (Path C / LRM-1027).

Distinct from SHELVED RecomputeFromArtifact (recompute from on-disk artifact),
SemanticNotProxy / Multistage / PreserveSemantics, PerfHeadroom / TimeBudget,
StopEnvReprobe / TrustCheck / WriteOnDiscovery, and from KEEP ForbiddenExtras /
IndependentValidator / Enumerate / SanitySmoke / APG.

Fires when contract work involves numeric limits / cancellation / ordering /
queued-vs-running edges but there is no evidence of a boundary-case check
(happy-path-only). Does not feed fixture answers (Tm deltas, anneal lengths).

Inject (Tess TWEAK lesson from Enumerate `2000`): on risk in `after_tool`,
write sidecar + append tool note with literal `BoundaryCaseCheckGate:`
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

STATE_KEY = "boundary_case_check_gate_state"
MARKER = "BoundaryCaseCheckGate:"
logger = logging.getLogger(__name__)

# Contract surfaces that imply a numeric / behavioral edge must be checked.
LIMIT_CONTRACT_RE = re.compile(
    r"(?:"
    r"\b(?:Tm|melting\s*temp(?:erature)?|primer)\b|"
    r"\b(?:anneal(?:ing)?|overhang|oligo)\b|"
    r"\b(?:limit|threshold|max(?:imum)?|min(?:imum)?|bound(?:ary)?|inclusive)\b|"
    r"\b(?:cancel(?:lation|led)?|timeout|deadline|queue(?:d)?|ordering|fifo)\b|"
    r"\b(?:queued[- ]vs[- ]running|off[- ]by[- ]one)\b|"
    r"[≤≥<>]=?\s*\d|"
    r"\b\d+\s*(?:°|deg(?:rees?)?|nt|bp|ms|s|sec(?:onds?)?)\b|"
    r"\bbetween\s+\d+\s+and\s+\d+\b|"
    r"\bat\s+most\b|\bat\s+least\b|\bno\s+more\s+than\b|\bno\s+less\s+than\b"
    r")",
    re.IGNORECASE,
)

# Deliverable writes / publishes that should not skip the edge check.
WRITE_RE = re.compile(
    r"(?:"
    r"(?:>>?|tee\b)\s*/app/|"
    r"\b(?:cp|mv|install|rsync)\b[^\n]+/app/|"
    r"cat\s*>\s*/app/|"
    r"\bprintf\b[^\n]*>\s*/app/|"
    r"\bpython3?\s+[^\n]*\bopen\([^)]*['\"]/app/|"
    r"json\.dump\(|write_text\(|Path\([^)]*\)\.write|"
    r"\.(?:write|writelines|dump|to_csv|savefig)\("
    r")",
    re.IGNORECASE,
)

# Happy-path-only / single nominal check language.
HAPPY_PATH_RE = re.compile(
    r"(?:"
    r"\bhappy\s*path\b|"
    r"\bnominal\s+(?:case|check|value)\b|"
    r"\bonly\s+(?:check|test|verify)\s+(?:the\s+)?(?:main|normal)\b|"
    r"\bskip(?:ping)?\s+(?:edge|boundary|limit)\b"
    r")",
    re.IGNORECASE,
)

# Evidence that a boundary / edge / limit case was actually exercised.
BOUNDARY_CHECK_RE = re.compile(
    r"(?:"
    r"\bboundary[-_ ]?case\b|"
    r"\bedge[-_ ]?case\b|"
    r"\bat[-_ ]?(?:the[-_ ]?)?limit\b|"
    r"\bnear[-_ ]?(?:the[-_ ]?)?limit\b|"
    r"\boff[-_ ]by[-_ ]one\b|"
    r"\b(?:just\s+)?(?:under|over|inside|outside)\s+(?:the\s+)?(?:limit|bound|threshold)\b|"
    r"\b(?:cancel(?:lation)?|queue(?:d)?|ordering)\s+(?:case|check|test|probe)\b|"
    r"\bassert\b[^\n]{0,160}"
    r"(?:<=|>=|<|>|==)\s*\d|"
    r"\b(?:len|abs|max|min)\s*\([^)]*\)\s*(?:<=|>=|<|>)\s*\d"
    r")",
    re.IGNORECASE,
)

_SIDECAR_CANDIDATES = (
    Path("/logs/agent/boundary_case_check.nudge.txt"),
    Path("boundary_case_check.nudge.txt"),
)


class BoundaryCaseCheckGateMiddleware(Middleware):
    """Nudge when limit/cancel/queue edges lack a boundary-case check."""

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
            "limit_events": 0,
            "write_events": 0,
            "happy_path_events": 0,
            "boundary_check_events": 0,
            "limit_without_boundary": 0,
            "write_without_boundary": 0,
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

        if BOUNDARY_CHECK_RE.search(blob):
            state["boundary_check_events"] = int(state["boundary_check_events"]) + 1
            state["last_reason"] = "boundary_case_check_ok"

        if LIMIT_CONTRACT_RE.search(blob):
            state["limit_events"] = int(state["limit_events"]) + 1
            if int(state["boundary_check_events"]) <= 0:
                state["limit_without_boundary"] = (
                    int(state["limit_without_boundary"]) + 1
                )
                state["last_reason"] = "limit_contract_without_boundary_case"

        if WRITE_RE.search(command) or WRITE_RE.search(blob):
            state["write_events"] = int(state["write_events"]) + 1
            if (
                int(state["limit_events"]) > 0
                and int(state["boundary_check_events"]) <= 0
            ):
                state["write_without_boundary"] = (
                    int(state["write_without_boundary"]) + 1
                )
                state["last_reason"] = "write_with_limit_contract_no_boundary_case"

        if HAPPY_PATH_RE.search(blob):
            state["happy_path_events"] = int(state["happy_path_events"]) + 1
            if int(state["boundary_check_events"]) <= 0:
                state["last_reason"] = "happy_path_only_without_boundary_case"

        reminder = ""
        tool_output = hook_input.tool_output
        limit_wo = int(state["limit_without_boundary"])
        write_wo = int(state["write_without_boundary"])
        happy = int(state["happy_path_events"])
        boundary = int(state["boundary_check_events"])
        risk = limit_wo + write_wo + (1 if happy > 0 and boundary <= 0 else 0)
        if (
            int(state["nudge_count"]) == 0
            and boundary <= 0
            and risk >= self.min_risk_events
            and (limit_wo > 0 or write_wo > 0 or happy > 0)
        ):
            reason = str(
                state.get("last_reason") or "limit_contract_without_boundary_case"
            )
            reminder = self._build_reminder(
                reason=reason,
                iteration=-1,
                limit_events=int(state["limit_events"]),
                write_events=int(state["write_events"]),
                happy_path_events=happy,
                boundary_check_events=boundary,
                limit_without_boundary=limit_wo,
                write_without_boundary=write_wo,
            )
            self._write_sidecar(reminder)
            state["nudge_count"] = 1
            state["last_nudge_iteration"] = 0
            state["nudge_fired"] = True
            state["sticky_reminder"] = reminder
            state["message_inject_pending"] = True
            tool_output = self._append_tool_note(tool_output, reminder)
            logger.info(
                "[BoundaryCaseCheckGateMiddleware] after_tool fire #1 reason=%s "
                "limit_wo=%s write_wo=%s happy=%s boundary=%s "
                "(sidecar+tool_note; pending message inject)",
                reason,
                limit_wo,
                write_wo,
                happy,
                boundary,
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
                    "[BoundaryCaseCheckGateMiddleware] Sticky SYSTEM/USER "
                    "re-apply iteration=%s system=%s user=%s pending=%s",
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

        limit_wo = int(state["limit_without_boundary"])
        write_wo = int(state["write_without_boundary"])
        happy = int(state["happy_path_events"])
        boundary = int(state["boundary_check_events"])
        if boundary > 0:
            return _passthrough()
        risk = limit_wo + write_wo + (1 if happy > 0 else 0)
        if risk < self.min_risk_events:
            return _passthrough()

        fire = False
        reason = state.get("last_reason") or ""
        if limit_wo > 0:
            fire = True
            reason = reason or "limit_contract_without_boundary_case"
        elif write_wo > 0:
            fire = True
            reason = reason or "write_with_limit_contract_no_boundary_case"
        elif happy > 0:
            fire = True
            reason = reason or "happy_path_only_without_boundary_case"

        if not fire:
            return _passthrough()

        state["nudge_count"] = int(state["nudge_count"]) + 1
        state["last_nudge_iteration"] = iteration
        state["nudge_fired"] = True
        reminder = self._build_reminder(
            reason=reason,
            iteration=iteration,
            limit_events=int(state["limit_events"]),
            write_events=int(state["write_events"]),
            happy_path_events=happy,
            boundary_check_events=boundary,
            limit_without_boundary=limit_wo,
            write_without_boundary=write_wo,
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
            "[BoundaryCaseCheckGateMiddleware] Nudge #%s reason=%s iteration=%s "
            "limit_wo=%s write_wo=%s happy=%s boundary=%s "
            "system_patched=%s user_patched=%s",
            state["nudge_count"],
            reason,
            iteration,
            limit_wo,
            write_wo,
            happy,
            boundary,
            system_patched,
            user_patched,
        )
        return HookResult.with_modifications(messages=updated_messages)

    def _build_reminder(
        self,
        *,
        reason: str,
        iteration: int,
        limit_events: int,
        write_events: int,
        happy_path_events: int,
        boundary_check_events: int,
        limit_without_boundary: int,
        write_without_boundary: int,
    ) -> str:
        return (
            f"{MARKER} {reason}; iteration={iteration}; "
            f"limit_events={limit_events}; "
            f"write_events={write_events}; "
            f"happy_path_events={happy_path_events}; "
            f"boundary_check_events={boundary_check_events}; "
            f"limit_without_boundary={limit_without_boundary}; "
            f"write_without_boundary={write_without_boundary}. "
            "If the contract includes limits, cancellation, ordering, or "
            "queued-vs-running behavior, include at least one boundary-case "
            "check that exercises that edge, not just the happy path. Re-check "
            "explicit numeric limits against the final candidate and nudge the "
            "artifact across the edge when it is close but not yet compliant. "
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
                    "[BoundaryCaseCheckGateMiddleware] Appended fire sidecar %s",
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
            "limit_events": int(state.get("limit_events", 0) or 0),
            "write_events": int(state.get("write_events", 0) or 0),
            "happy_path_events": int(state.get("happy_path_events", 0) or 0),
            "boundary_check_events": int(state.get("boundary_check_events", 0) or 0),
            "limit_without_boundary": int(state.get("limit_without_boundary", 0) or 0),
            "write_without_boundary": int(state.get("write_without_boundary", 0) or 0),
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
