"""Persist-best-candidate gate — AHE §4 (Path C / LRM-976).

Distinct from SHELVED PublishPressure (iteration clock), CheckpointDeadline
(wall-clock), EndStateReady (env end state), and ScratchThenPromote (layout).
Fires when expensive search/train/autotune has produced a usable best-so-far
only in logs/tmp/scratch/RAM while the contract deliverable path is still
empty/stale and the agent is about to continue another long run.
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

STATE_KEY = "persist_best_candidate_gate_state"
logger = logging.getLogger(__name__)

DELIVERABLE_PATH_RE = re.compile(r"(/app/[A-Za-z0-9_./-]+)")
REQUIRED_HINT_RE = re.compile(
    r"(?:stored in|write(?:n)? to|save(?:d)? (?:to|as)|output(?: file)?|"
    r"file titled|create(?:s|d)?|produce(?:s|d)?|model(?: file)?)"
    r"[^\n]{0,40}?(/app/[A-Za-z0-9_./-]+)",
    re.IGNORECASE,
)
WRITE_CMD_RE = re.compile(
    r"(?:"
    r"(?:>>?|tee\b|cat\s*>)\s*[^\n]*(/app/[A-Za-z0-9_./-]+)|"
    r"(?:cp|mv|install)\b[^\n]+(/app/[A-Za-z0-9_./-]+)|"
    r"write_text\s*\(\s*['\"](/app/[A-Za-z0-9_./-]+)['\"]|"
    r"open\s*\(\s*['\"](/app/[A-Za-z0-9_./-]+)['\"][^)]*['\"]w|"
    r"(?:\.save|torch\.save|joblib\.dump|pickle\.dump|json\.dump|"
    r"to_csv|to_json|fasttext\.train)\s*\([^\n]*(/app/[A-Za-z0-9_./-]+)"
    r")",
    re.IGNORECASE,
)
SCRATCH_WRITE_RE = re.compile(
    r"(?:"
    r"(?:>>?|tee\b|cp\b|mv\b|install\b|write_text|open\([^)]*['\"]w|"
    r"\.save\(|torch\.save|joblib\.dump|pickle\.dump)"
    r"[^\n]*(?:/tmp/|/var/tmp/|\./tmp/|notebooks?/|\.ipynb|/\.cache/)"
    r"|"
    r"(?:/tmp/|/var/tmp/)[A-Za-z0-9_./-]+"
    r")",
    re.IGNORECASE,
)
LONG_WORK_RE = re.compile(
    r"\b(?:train(?:ing)?|optimize|solver|epochs?|fit\(|caffe|mujoco|"
    r"search\b|hyperopt|grid.?search|bayes|fine.?tun|autotune|"
    r"fasttext\.train|sweep|random.?search|optuna)\b",
    re.IGNORECASE,
)
_SIDECAR_CANDIDATES = (
    Path("/logs/agent/persist_best_candidate.nudge.txt"),
    Path("persist_best_candidate.nudge.txt"),
)


class PersistBestCandidateGateMiddleware(Middleware):
    """Nudge to persist best-so-far to the contract path before more long runs."""

    def __init__(
        self,
        *,
        min_iterations_before_nudge: int = 3,
        renudge_every_iterations: int = 8,
        max_nudges: int = 3,
        long_step_ms: int = 60_000,
        min_long_work_events: int = 1,
    ) -> None:
        self.min_iterations_before_nudge = int(min_iterations_before_nudge)
        self.renudge_every_iterations = int(renudge_every_iterations)
        self.max_nudges = int(max_nudges)
        self.long_step_ms = int(long_step_ms)
        self.min_long_work_events = int(min_long_work_events)

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
            "published_paths": [],
            "scratch_writes": 0,
            "long_work_events": 0,
            "long_ms": 0,
            "nudge_count": 0,
            "last_nudge_iteration": -10_000,
            "nudge_fired": False,
            "last_reason": "",
            "sticky_reminder": "",
        }
        hook_input.agent_state.set_global_value(STATE_KEY, self._dump_state(state))
        if required:
            logger.info(
                "[PersistBestCandidateGateMiddleware] Required paths from task: %s",
                sorted(required),
            )
        return HookResult.no_changes()

    def after_tool(self, hook_input: AfterToolHookInput) -> HookResult:
        if hook_input.tool_name != "run_shell_command":
            return HookResult.no_changes()

        tool_input = hook_input.tool_input if isinstance(hook_input.tool_input, dict) else {}
        command = str(tool_input.get("command", ""))
        state = self._load_state(hook_input.agent_state.get_global_value(STATE_KEY, {}))
        published = set(state["published_paths"])
        required = set(state["required_paths"])

        for match in WRITE_CMD_RE.finditer(command):
            path = next((g for g in match.groups() if g), None)
            if not path:
                continue
            path = path.rstrip(".,;)'\"")
            if path in required or path.startswith("/app/"):
                published.add(path)
                state["last_reason"] = f"contract_publish:{path}"

        if SCRATCH_WRITE_RE.search(command):
            state["scratch_writes"] = int(state["scratch_writes"]) + 1
            state["last_reason"] = "scratch_or_tmp_candidate_write"

        duration_ms = self._extract_duration_ms(hook_input.tool_output)
        if duration_ms >= self.long_step_ms or LONG_WORK_RE.search(command):
            state["long_work_events"] = int(state["long_work_events"]) + 1
            if duration_ms >= self.long_step_ms:
                state["long_ms"] = int(state["long_ms"]) + max(duration_ms, 0)
            if not state.get("last_reason"):
                state["last_reason"] = "long_work_without_contract_publish"

        state["published_paths"] = sorted(published)
        hook_input.agent_state.set_global_value(STATE_KEY, self._dump_state(state))
        return HookResult.no_changes()

    def before_model(self, hook_input: BeforeModelHookInput) -> HookResult:
        iteration = int(hook_input.current_iteration or 0)
        state = self._load_state(hook_input.agent_state.get_global_value(STATE_KEY, {}))
        sticky = str(state.get("sticky_reminder") or "")
        messages = list(hook_input.messages)
        sticky_applied = False

        # Tess TWEAK `0017`: sticky re-paint — SYSTEM PREPEND/INSERT + original
        # USER PREPEND when the literal marker is missing (cleaned tracers drop
        # mid-run USER appends / FRAMEWORK; LocalContext-style SYSTEM + durable
        # first-USER land on cleaned.json).
        if sticky and not self._messages_have_marker(messages, "PersistBestCandidateGate:"):
            messages, sys_ok = self._apply_system_marker(messages, sticky)
            messages, user_ok = self._apply_user_marker(messages, sticky)
            sticky_applied = sys_ok or user_ok
            if sticky_applied:
                logger.info(
                    "[PersistBestCandidateGateMiddleware] Sticky re-apply "
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

        if int(state["long_work_events"]) < self.min_long_work_events:
            return _passthrough()

        required = set(state["required_paths"])
        published = set(state["published_paths"])
        missing = sorted(required - published) if required else []
        # Fire when long work happened and contract path still empty, especially
        # if a scratch/tmp candidate write was seen (or long work alone).
        if required and not missing:
            return _passthrough()
        if not required and int(state["scratch_writes"]) == 0:
            # No contract paths parsed and no scratch signal — stay quiet.
            return _passthrough()
        if required and missing and int(state["scratch_writes"]) == 0:
            # Still nudge: long work with empty contract path is the §4 failure.
            pass

        reason = state.get("last_reason") or "best_left_off_contract_path"
        state["nudge_count"] += 1
        state["last_nudge_iteration"] = iteration
        state["nudge_fired"] = True

        missing_s = ", ".join((missing or sorted(required) or ["<contract path>"])[:4])
        reminder = (
            f"PersistBestCandidateGate: {reason}; iteration={iteration}; "
            f"long_work_events={state['long_work_events']}; "
            f"scratch_writes={state['scratch_writes']}; "
            f"missing_contract={missing_s}. "
            "A usable best-so-far must be copied/saved onto the exact evaluator "
            "deliverable path before another long search/train/autotune. Do not "
            "leave the only candidate in logs, /tmp, notebooks, or RAM. Do not "
            "invent domain tips from this reminder."
        )
        self._write_sidecar(reminder)
        state["sticky_reminder"] = reminder
        hook_input.agent_state.set_global_value(STATE_KEY, self._dump_state(state))

        # Tess TWEAK `0017`: SYSTEM PREPEND if present else INSERT; PREPEND into
        # first USER (cleaned-durable); sticky re-paint; keep trailing USER +
        # sidecar (禁 USER-only / 禁只靠 sidecar).
        updated_messages, system_patched = self._apply_system_marker(messages, reminder)
        updated_messages, user_patched = self._apply_user_marker(
            updated_messages, reminder
        )
        updated_messages.append(Message(role=Role.USER, content=[TextBlock(text=reminder)]))
        logger.info(
            "[PersistBestCandidateGateMiddleware] Nudge #%s reason=%s iteration=%s "
            "long=%s scratch=%s missing=%s system_patched=%s user_patched=%s",
            state["nudge_count"],
            reason,
            iteration,
            state["long_work_events"],
            state["scratch_writes"],
            missing,
            system_patched,
            user_patched,
        )
        return HookResult.with_modifications(messages=updated_messages)

    def _extract_duration_ms(self, tool_output: Any) -> int:
        text = ""
        if isinstance(tool_output, str):
            text = tool_output
        elif tool_output is not None:
            text = str(tool_output)
        match = re.search(r"(?:duration|elapsed|took)[^\d]{0,12}(\d+(?:\.\d+)?)\s*(ms|s)\b", text, re.I)
        if not match:
            return 0
        value = float(match.group(1))
        unit = match.group(2).lower()
        return int(value if unit == "ms" else value * 1000)

    def _messages_have_marker(self, messages: list[Message], marker: str) -> bool:
        """True if marker is on SYSTEM or the first USER (cleaned-durable surfaces)."""
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
        """PREPEND into first SYSTEM if present; else INSERT SYSTEM at front."""
        updated_messages: list[Message] = []
        system_patched = False
        for message in messages:
            role = getattr(message, "role", None)
            if not system_patched and (
                role == Role.SYSTEM or self._is_system_role(message)
            ):
                existing = ""
                get_text = getattr(message, "get_text_content", None)
                if callable(get_text):
                    existing = get_text() or ""
                else:
                    existing = self._message_text(message)
                if "PersistBestCandidateGate:" not in existing:
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
        """PREPEND into the first USER message (survives cleaned export)."""
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
                if "PersistBestCandidateGate:" not in existing:
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
                    "[PersistBestCandidateGateMiddleware] Appended fire sidecar %s",
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
            "required_paths": sorted(
                {str(p) for p in (state.get("required_paths") or []) if p}
            ),
            "published_paths": sorted(
                {str(p) for p in (state.get("published_paths") or []) if p}
            ),
            "scratch_writes": int(state.get("scratch_writes", 0) or 0),
            "long_work_events": int(state.get("long_work_events", 0) or 0),
            "long_ms": int(state.get("long_ms", 0) or 0),
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
