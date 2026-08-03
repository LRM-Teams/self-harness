"""Preserve-semantics / minimal-diff gate — AHE §3 (Path C / LRM-997).

Distinct from SHELVED ScratchThenPromote (scratch→copy path),
RereadDeliverable (disk reread), LiteralContract (token copy),
StopAfterPass, FailFast, ShortProbeFirst, PersistBest, TimeBudget, PP.

Fires when the agent broad-rewrites, applies blanket transforms, or cleans
up already-restored /app artifacts without proof — process pressure only,
never domain tips or fixture answers.
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

STATE_KEY = "preserve_semantics_gate_state"
MARKER = "PreserveSemanticsGate:"
logger = logging.getLogger(__name__)

APP_PATH_RE = re.compile(r"(/app/[A-Za-z0-9_./-]+)")
# Broad destructive / rewrite patterns under contract paths.
# (Normal surgical `cat > /app/...` writes are NOT broad — those are expected.)
BROAD_REWRITE_RE = re.compile(
    r"(?:"
    r"\brm\s+-rf\b[^\n]*/app/|"
    r"\bfind\b[^\n]*/app/[^\n]*(?:-delete|-exec\s+rm\b)|"
    r"\btruncate\s+-s\s+0\b[^\n]*/app/|"
    r"\bcp\s+-rf?\s+/dev/null\b|"
    r":\s*>\s*/app/[A-Za-z0-9_./-]+|"
    r"\bdd\s+if=/dev/(?:zero|null)\b[^\n]*/app/"
    r")",
    re.IGNORECASE,
)
# Blanket transforms that can change correct edge cases.
BLANKET_TRANSFORM_RE = re.compile(
    r"(?:"
    r"\bsed\s+-i\b|"
    r"\bperl\s+-pi\b|"
    r"\bfind\b[^\n]*/app/[^\n]*-exec\s+(?:sed|perl)\b|"
    r"\bblack\b|\bautopep8\b|\bisort\b|\bruff\s+format\b|"
    r"\bclang-format\s+-i\b|"
    r"\bdos2unix\b|\bunix2dos\b"
    r")",
    re.IGNORECASE,
)
# Cleanup of restored/written artifacts without a failing check as cause.
CLEANUP_RE = re.compile(
    r"(?:"
    r"\brm\s+(?:-f\s+)?(?:-r\s+)?[^\n]*/app/|"
    r"\bunlink\b[^\n]*/app/|"
    r"\bshutil\.rmtree\b|"
    r"\bos\.remove\b"
    r")",
    re.IGNORECASE,
)
# Write/restore of /app paths (evidence that later cleanup is risky).
WRITE_RESTORE_RE = re.compile(
    r"(?:"
    r"(?:>>?|tee\b)\s*/app/|"
    r"\b(?:cp|mv|install|rsync)\b[^\n]+/app/|"
    r"\bgit\s+(?:checkout|restore|cherry-pick|apply|am)\b|"
    r"\btar\s+-x|"
    r"\bunzip\b|\bgunzip\b"
    r")",
    re.IGNORECASE,
)
# Experiments living under constrained /app instead of scratch.
APP_EXPERIMENT_RE = re.compile(
    r"(?:"
    r"/app/(?:tmp|temp|scratch|candidate|experiment|build_tmp|out_tmp)/|"
    r"\bmkdir\s+-p\s+/app/(?:tmp|temp|scratch|candidate)"
    r")",
    re.IGNORECASE,
)

_SIDECAR_CANDIDATES = (
    Path("/logs/agent/preserve_semantics.nudge.txt"),
    Path("preserve_semantics.nudge.txt"),
)


class PreserveSemanticsGateMiddleware(Middleware):
    """Nudge when edits look broad / cleanup-risky instead of surgical."""

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
            "broad_rewrite_events": 0,
            "blanket_transform_events": 0,
            "cleanup_after_write_events": 0,
            "app_experiment_events": 0,
            "write_restore_events": 0,
            "written_paths": [],
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
        written = set(state.get("written_paths") or [])

        if WRITE_RESTORE_RE.search(command):
            state["write_restore_events"] = int(state["write_restore_events"]) + 1
            for path in APP_PATH_RE.findall(command):
                written.add(path.rstrip(".,;)'\""))
            state["last_reason"] = "write_or_restore"

        if BROAD_REWRITE_RE.search(command):
            state["broad_rewrite_events"] = int(state["broad_rewrite_events"]) + 1
            state["last_reason"] = "broad_rewrite_under_app"

        if BLANKET_TRANSFORM_RE.search(command) and (
            "/app/" in command or bool(APP_PATH_RE.search(command))
        ):
            state["blanket_transform_events"] = int(state["blanket_transform_events"]) + 1
            state["last_reason"] = "blanket_transform_under_app"

        if CLEANUP_RE.search(command):
            touched = {p.rstrip(".,;)'\"") for p in APP_PATH_RE.findall(command)}
            if written & touched or "/app/" in command:
                state["cleanup_after_write_events"] = (
                    int(state["cleanup_after_write_events"]) + 1
                )
                state["last_reason"] = "cleanup_after_write_or_restore"

        if APP_EXPERIMENT_RE.search(command):
            state["app_experiment_events"] = int(state["app_experiment_events"]) + 1
            state["last_reason"] = "experiment_under_constrained_app"

        state["written_paths"] = sorted(written)
        hook_input.agent_state.set_global_value(STATE_KEY, self._dump_state(state))
        return HookResult.no_changes()

    def before_model(self, hook_input: BeforeModelHookInput) -> HookResult:
        iteration = int(hook_input.current_iteration or 0)
        state = self._load_state(hook_input.agent_state.get_global_value(STATE_KEY, {}))
        sticky = str(state.get("sticky_reminder") or "")
        messages = list(hook_input.messages)
        sticky_applied = False

        # Align ShortProbe t2: sticky re-paints SYSTEM (cleaned-durable).
        if sticky and not self._system_has_marker(messages, MARKER):
            messages, sys_ok = self._apply_system_marker(messages, sticky)
            messages, user_ok = self._apply_user_marker(messages, sticky)
            sticky_applied = sys_ok or user_ok
            if sticky_applied:
                logger.info(
                    "[PreserveSemanticsGateMiddleware] Sticky re-apply "
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

        broad = int(state["broad_rewrite_events"])
        blanket = int(state["blanket_transform_events"])
        cleanup = int(state["cleanup_after_write_events"])
        app_exp = int(state["app_experiment_events"])
        risk = broad + blanket + cleanup + app_exp
        if risk < self.min_risk_events:
            return _passthrough()

        fire = False
        reason = state.get("last_reason") or ""
        if broad > 0:
            fire = True
            reason = reason or "broad_rewrite_under_app"
        elif blanket > 0:
            fire = True
            reason = reason or "blanket_transform_under_app"
        elif cleanup > 0:
            fire = True
            reason = reason or "cleanup_after_write_or_restore"
        elif app_exp > 0:
            fire = True
            reason = reason or "experiment_under_constrained_app"

        if not fire:
            return _passthrough()

        state["nudge_count"] += 1
        state["last_nudge_iteration"] = iteration
        state["nudge_fired"] = True

        reminder = (
            f"{MARKER} {reason}; iteration={iteration}; "
            f"broad_rewrite_events={broad}; blanket_transform_events={blanket}; "
            f"cleanup_after_write_events={cleanup}; "
            f"app_experiment_events={app_exp}. "
            "Preserve semantics; keep changes minimal: fix the specific bug "
            "without broad rewrites; preserve existing public behavior; avoid "
            "blanket validation/global transforms that change correct edge "
            "cases; do not clean up already-restored files unless an "
            "acceptance check proves it necessary; keep experiments in /tmp "
            "or a disposable subdirectory and copy only the verified final "
            "artifact into the deliverable location. This is not a "
            "scratch→promote checklist and not a request to invent domain tips."
        )
        self._write_sidecar(reminder)
        state["sticky_reminder"] = reminder
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
            "[PreserveSemanticsGateMiddleware] Nudge #%s reason=%s iteration=%s "
            "broad=%s blanket=%s cleanup=%s app_exp=%s system_patched=%s "
            "user_patched=%s",
            state["nudge_count"],
            reason,
            iteration,
            broad,
            blanket,
            cleanup,
            app_exp,
            system_patched,
            user_patched,
        )
        return HookResult.with_modifications(messages=updated_messages)

    def _system_has_marker(self, messages: list[Message], marker: str) -> bool:
        for message in messages:
            if self._is_system_role(message) and marker in self._message_text(message):
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
                    "[PreserveSemanticsGateMiddleware] Appended fire sidecar %s",
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
            "broad_rewrite_events": int(state.get("broad_rewrite_events", 0) or 0),
            "blanket_transform_events": int(
                state.get("blanket_transform_events", 0) or 0
            ),
            "cleanup_after_write_events": int(
                state.get("cleanup_after_write_events", 0) or 0
            ),
            "app_experiment_events": int(state.get("app_experiment_events", 0) or 0),
            "write_restore_events": int(state.get("write_restore_events", 0) or 0),
            "written_paths": sorted(
                {str(p) for p in (state.get("written_paths") or []) if p}
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
