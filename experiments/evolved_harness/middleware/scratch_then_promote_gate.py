"""Scratch-then-promote gate — AHE §3 (Path C / LRM-968).

Distinct from SHELVED CanonicalEntry (re-run from public entry after scratch
checks), StopAfterPass (stop polish after eval pass), and PreCompletion
(pre-finish checklist). Fires when the agent keeps iterating writes/builds
in-place under contract paths (/app) without prototyping in scratch and
promoting only the verified final artifact.
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

STATE_KEY = "scratch_then_promote_gate_state"
logger = logging.getLogger(__name__)

WRITE_OR_BUILD_RE = re.compile(
    r"(?:>>?|tee\b|install\b|cp\b|mv\b|cat\s*>|\bgcc\b|\bg\+\+|\bclang\b|\brustc\b|"
    r"\bmake\b|\bcargo\s+build|\bnim\s+c\b|write_text|open\([^)]*['\"]w|"
    r"sed\s+-i|perl\s+-i|\bnano\b|\bvim?\b|\bcmake\b|\bpython3?\s+-m\s+pip\s+install)",
    re.IGNORECASE,
)
CONTRACT_PATH_RE = re.compile(r"(?:/app/|\./|/home/[^/\s]+/|/workdir/)", re.IGNORECASE)
SCRATCH_RE = re.compile(
    r"(?:/tmp/|/scratch/|\bscratch[_-]|\bmktemp\b|\bTMPDIR=)",
    re.IGNORECASE,
)
# Promote: copy/move from scratch into contract path.
PROMOTE_RE = re.compile(
    r"(?:(?:cp|mv|install)\b[^\n]*(?:/tmp/|/scratch/)[^\n]*(?:/app/|\./)|"
    r"(?:cp|mv|install)\b[^\n]*\b(?:/app/|\./)[^\n]*(?:/tmp/|/scratch/))",
    re.IGNORECASE,
)
# Clear promote direction: scratch → /app
PROMOTE_TO_CONTRACT_RE = re.compile(
    r"(?:cp|mv|install)\b[^\n]*(?:/tmp/|/scratch/)[^\n]+(?:/app/)",
    re.IGNORECASE,
)

_SIDECAR_CANDIDATES = (
    Path("/logs/agent/scratch_then_promote.nudge.txt"),
    Path("scratch_then_promote.nudge.txt"),
)


class ScratchThenPromoteGateMiddleware(Middleware):
    """Nudge to prototype in scratch, then promote verified finals to /app."""

    def __init__(
        self,
        *,
        min_iterations_before_nudge: int = 3,
        renudge_every_iterations: int = 8,
        max_nudges: int = 3,
        min_in_place_events: int = 2,
    ) -> None:
        self.min_iterations_before_nudge = int(min_iterations_before_nudge)
        self.renudge_every_iterations = int(renudge_every_iterations)
        self.max_nudges = int(max_nudges)
        self.min_in_place_events = int(min_in_place_events)

    def before_agent(self, hook_input: BeforeAgentHookInput) -> HookResult:
        state = {
            "in_place_events": 0,
            "scratch_seen": False,
            "promote_seen": False,
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
        state = self._load_state(hook_input.agent_state.get_global_value(STATE_KEY, {}))

        if SCRATCH_RE.search(command) and WRITE_OR_BUILD_RE.search(command):
            state["scratch_seen"] = True
            state["last_reason"] = "scratch_work_seen"

        if PROMOTE_TO_CONTRACT_RE.search(command) or (
            PROMOTE_RE.search(command) and "/app/" in command and SCRATCH_RE.search(command)
        ):
            state["promote_seen"] = True
            state["last_reason"] = "promote_to_contract_seen"

        # In-place iteration: write/build touching contract paths without a
        # scratch→promote pattern in the same command.
        if (
            WRITE_OR_BUILD_RE.search(command)
            and CONTRACT_PATH_RE.search(command)
            and not PROMOTE_TO_CONTRACT_RE.search(command)
        ):
            # Pure scratch-only commands should not count as in-place.
            if not (SCRATCH_RE.search(command) and "/app/" not in command):
                if "/app/" in command or re.search(r"(?:^|[;&|])\s*(?:make|gcc|g\+\+|clang)\b", command):
                    state["in_place_events"] = int(state["in_place_events"]) + 1
                    state["last_reason"] = "in_place_write_or_build"

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

        # Fire when in-place iteration is happening and a verified promote
        # discipline is not yet evidenced.
        if int(state["in_place_events"]) < self.min_in_place_events:
            return HookResult.no_changes()
        if state["promote_seen"] and state["scratch_seen"]:
            return HookResult.no_changes()

        reason = state.get("last_reason") or "in_place_without_scratch_promote"
        state["nudge_count"] += 1
        state["last_nudge_iteration"] = iteration
        state["nudge_fired"] = True
        hook_input.agent_state.set_global_value(STATE_KEY, self._dump_state(state))

        reminder = (
            f"ScratchThenPromoteGate: {reason}; iteration={iteration}; "
            f"in_place_events={state['in_place_events']}. "
            "Prototype experiments and candidate builds in scratch (/tmp or a "
            "disposable subdirectory). After verification, copy only the "
            "verified final artifact into the contract deliverable location. "
            "Do not keep iterating write/compile/install directly under the "
            "constrained deliverable paths. Do not invent domain tips from "
            "this reminder."
        )
        self._write_sidecar(reminder)

        # Tess TWEAK `1751`: sidecar fired but cleaned tracer `ScratchThenPromoteGate:`=0.
        # Mid-run before_model often has no SYSTEM in `messages` (system_prompt is
        # frozen separately) — append-only then becomes USER-only and cleaned drops
        # mid-run USER. Align LocalContext/OfficialChecker t2: PREPEND into SYSTEM
        # when present; if absent, INSERT a SYSTEM message so the literal marker
        # lands on the cleaned system/user visible surface. Keep USER + sidecar.
        updated_messages: list[Message] = []
        system_patched = False
        for message in hook_input.messages:
            if not system_patched and self._is_system_role(message):
                existing = ""
                get_text = getattr(message, "get_text_content", None)
                if callable(get_text):
                    existing = get_text() or ""
                else:
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
        updated_messages.append(Message(role=Role.USER, content=[TextBlock(text=reminder)]))
        logger.info(
            "[ScratchThenPromoteGateMiddleware] Nudge #%s reason=%s iteration=%s "
            "in_place=%s scratch=%s promote=%s system_patched=%s",
            state["nudge_count"],
            reason,
            iteration,
            state["in_place_events"],
            state["scratch_seen"],
            state["promote_seen"],
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
                    "[ScratchThenPromoteGateMiddleware] Appended fire sidecar %s", path
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
            "in_place_events": int(state.get("in_place_events", 0) or 0),
            "scratch_seen": bool(state.get("scratch_seen", False)),
            "promote_seen": bool(state.get("promote_seen", False)),
            "nudge_count": int(state.get("nudge_count", 0) or 0),
            "last_nudge_iteration": int(state.get("last_nudge_iteration", -10_000) or -10_000),
            "nudge_fired": bool(state.get("nudge_fired", False)),
            "last_reason": str(state.get("last_reason") or ""),
        }

    def _load_state(self, raw_state: Any) -> dict[str, Any]:
        return self._dump_state(raw_state if isinstance(raw_state, dict) else {})
