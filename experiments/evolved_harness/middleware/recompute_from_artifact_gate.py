"""Recompute-from-artifact gate — AHE §2 close-the-loop (Path C / LRM-1020).

Distinct from SHELVED RereadDeliverable (re-read / re-invoke entry), EndStateReady,
CanonicalEntry, MirrorEvaluator / ExactSourceLayout, SemanticNotProxy /
PreserveSemantics / §6 timeout-publish family, and from KEEP ForbiddenExtras
(pollution sweep) / IndependentValidator / Enumerate / SanitySmoke / APG.

Fires when a boundary-sensitive deliverable is written or checked using
design-time / earlier-candidate numbers without evidence that bounds were
recomputed from the final on-disk artifact / public path.

Inject (Tess TWEAK lesson from Enumerate `2000`): on risk in `after_tool`,
write sidecar + append tool note with literal `RecomputeFromArtifactGate:`
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

STATE_KEY = "recompute_from_artifact_gate_state"
MARKER = "RecomputeFromArtifactGate:"
logger = logging.getLogger(__name__)

# Writes / publishes of contract-facing artifacts.
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

# Boundary-sensitive compute / fit / frame / Tm / annealing work.
BOUNDARY_WORK_RE = re.compile(
    r"(?:"
    r"\b(?:Tm|melting\s*temp|primer|anneal(?:ing)?|overhang)\b|"
    r"\b(?:takeoff|landing|frame|fps|timestamp)\b|"
    r"\b(?:fit|curve_fit|optimize|least_squares|peak)\b|"
    r"\b(?:x0|gamma|offset|threshold|bound|inclusive\s+range)\b|"
    r"\bnp\.(?:polyfit|interp|argmin|argmax)\b"
    r")",
    re.IGNORECASE,
)

# Design-time / earlier-candidate checks that skip disk recompute.
DESIGN_TIME_RE = re.compile(
    r"(?:"
    r"\bdesign[-_ ]time\b|"
    r"\bearlier\s+candidate\b|"
    r"\bfrom\s+(?:memory|variables?|prior\s+state)\b|"
    r"\b(?:expected_|hardcoded_|cached_)\w+\b|"
    r"\bassert\b[^\n]{0,120}\b(?:tm|frame|anneal|peak|x0)\b[^\n]{0,80}"
    r"(?!.*(open\(|read_text|Path\([^)]*\)\.read|/app/))"
    r")",
    re.IGNORECASE,
)

# Evidence that bounds were recomputed from the submitted on-disk artifact.
RECOMPUTE_RE = re.compile(
    r"(?:"
    r"\brecompute\b|\bfrom\s+(?:disk|artifact|submitted\s+file)\b|"
    r"\bon[-_ ]disk\b|\bfinal\s+(?:artifact|deliverable)\b|"
    r"\b(?:cat|od|xxd|hexdump|nl|sha256sum)\s+/app/|"
    r"\bpython3?\s+-c\s+['\"][^'\"]{0,240}"
    r"(?:open\([^)]*/app/|Path\([^)]*/app/|read_text\()"
    r"[^'\"]{0,240}"
    r"(?:Tm|tm|anneal|frame|takeoff|fit|peak|bound|len\(|float\()"
    r")",
    re.IGNORECASE,
)

_SIDECAR_CANDIDATES = (
    Path("/logs/agent/recompute_from_artifact.nudge.txt"),
    Path("recompute_from_artifact.nudge.txt"),
)


class RecomputeFromArtifactGateMiddleware(Middleware):
    """Nudge when boundary checks skip recompute from the final artifact."""

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
            "boundary_events": 0,
            "design_time_events": 0,
            "recompute_events": 0,
            "write_without_recompute": 0,
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

        if RECOMPUTE_RE.search(blob):
            state["recompute_events"] = int(state["recompute_events"]) + 1
            state["last_reason"] = "recompute_from_artifact_ok"

        if WRITE_RE.search(command) or WRITE_RE.search(blob):
            state["write_events"] = int(state["write_events"]) + 1
            if int(state["recompute_events"]) <= 0:
                state["write_without_recompute"] = (
                    int(state["write_without_recompute"]) + 1
                )
                state["last_reason"] = "write_without_artifact_recompute"
            else:
                state["last_reason"] = "write_with_prior_or_later_recompute"

        if BOUNDARY_WORK_RE.search(blob):
            state["boundary_events"] = int(state["boundary_events"]) + 1
            if int(state["recompute_events"]) <= 0:
                state["last_reason"] = "boundary_work_without_artifact_recompute"

        if DESIGN_TIME_RE.search(blob):
            state["design_time_events"] = int(state["design_time_events"]) + 1
            if int(state["recompute_events"]) <= 0:
                state["last_reason"] = "design_time_check_without_recompute"

        reminder = ""
        tool_output = hook_input.tool_output
        write_wo = int(state["write_without_recompute"])
        boundary = int(state["boundary_events"])
        design = int(state["design_time_events"])
        recompute = int(state["recompute_events"])
        risk = write_wo + (
            1 if boundary > 0 and recompute <= 0 else 0
        ) + (1 if design > 0 and recompute <= 0 else 0)
        if (
            int(state["nudge_count"]) == 0
            and recompute <= 0
            and risk >= self.min_risk_events
            and (write_wo > 0 or boundary > 0 or design > 0)
        ):
            reason = str(
                state.get("last_reason") or "boundary_work_without_artifact_recompute"
            )
            reminder = self._build_reminder(
                reason=reason,
                iteration=-1,
                write_events=int(state["write_events"]),
                boundary_events=boundary,
                design_time_events=design,
                recompute_events=recompute,
                write_without_recompute=write_wo,
            )
            self._write_sidecar(reminder)
            state["nudge_count"] = 1
            state["last_nudge_iteration"] = 0
            state["nudge_fired"] = True
            state["sticky_reminder"] = reminder
            state["message_inject_pending"] = True
            tool_output = self._append_tool_note(tool_output, reminder)
            logger.info(
                "[RecomputeFromArtifactGateMiddleware] after_tool fire #1 reason=%s "
                "write_wo=%s boundary=%s design=%s recompute=%s "
                "(sidecar+tool_note; pending message inject)",
                reason,
                write_wo,
                boundary,
                design,
                recompute,
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
                    "[RecomputeFromArtifactGateMiddleware] Sticky SYSTEM/USER "
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

        write_wo = int(state["write_without_recompute"])
        boundary = int(state["boundary_events"])
        design = int(state["design_time_events"])
        recompute = int(state["recompute_events"])
        if recompute > 0:
            return _passthrough()
        risk = write_wo + (1 if boundary > 0 else 0) + (1 if design > 0 else 0)
        if risk < self.min_risk_events:
            return _passthrough()

        fire = False
        reason = state.get("last_reason") or ""
        if design > 0:
            fire = True
            reason = reason or "design_time_check_without_recompute"
        elif boundary > 0:
            fire = True
            reason = reason or "boundary_work_without_artifact_recompute"
        elif write_wo > 0:
            fire = True
            reason = reason or "write_without_artifact_recompute"

        if not fire:
            return _passthrough()

        state["nudge_count"] = int(state["nudge_count"]) + 1
        state["last_nudge_iteration"] = iteration
        state["nudge_fired"] = True
        reminder = self._build_reminder(
            reason=reason,
            iteration=iteration,
            write_events=int(state["write_events"]),
            boundary_events=boundary,
            design_time_events=design,
            recompute_events=recompute,
            write_without_recompute=write_wo,
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
            "[RecomputeFromArtifactGateMiddleware] Nudge #%s reason=%s iteration=%s "
            "write_wo=%s boundary=%s design=%s recompute=%s "
            "system_patched=%s user_patched=%s",
            state["nudge_count"],
            reason,
            iteration,
            write_wo,
            boundary,
            design,
            recompute,
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
        boundary_events: int,
        design_time_events: int,
        recompute_events: int,
        write_without_recompute: int,
    ) -> str:
        return (
            f"{MARKER} {reason}; iteration={iteration}; "
            f"write_events={write_events}; "
            f"boundary_events={boundary_events}; "
            f"design_time_events={design_time_events}; "
            f"recompute_events={recompute_events}; "
            f"write_without_recompute={write_without_recompute}. "
            "Close the loop from the final on-disk artifact itself: recompute "
            "boundary-sensitive checks (paths, annealing spans, serialized "
            "formatting, layout, frame/Tm bounds, etc.) from the submitted file "
            "or public path, not from design-time variables or an earlier "
            "candidate state. This is not a request to invent domain tips or "
            "fixture answers."
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
                    "[RecomputeFromArtifactGateMiddleware] Appended fire sidecar %s",
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
            "boundary_events": int(state.get("boundary_events", 0) or 0),
            "design_time_events": int(state.get("design_time_events", 0) or 0),
            "recompute_events": int(state.get("recompute_events", 0) or 0),
            "write_without_recompute": int(
                state.get("write_without_recompute", 0) or 0
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
