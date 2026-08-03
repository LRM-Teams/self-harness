"""Soft wall-clock finalize nudges — publish best candidate before AgentTimeout.

Distinct from TimeBudgetMiddleware (SHELVE'd HARD@900s / train×TB): no command
rewrite, no force_stop. Only FRAMEWORK nudges when remaining time crosses
milestones and required deliverables have not been written yet.
"""

from __future__ import annotations

import logging
import re
import time
from typing import Any

from nexau.archs.main_sub.execution.hooks import (
    AfterToolHookInput,
    BeforeAgentHookInput,
    BeforeModelHookInput,
    HookResult,
    Middleware,
)
from nexau.core.messages import Message, Role, TextBlock

STATE_KEY = "checkpoint_deadline_state"
logger = logging.getLogger(__name__)

DELIVERABLE_PATH_RE = re.compile(r"(/app/[A-Za-z0-9_./-]+)")
WRITE_CMD_RE = re.compile(
    r"(?:>|tee\b|cp\b|mv\b|install\b|write_text|open\([^)]*['\"]w|"
    r"\.save\(|torch\.save|pickle\.dump)",
    re.IGNORECASE,
)
REQUIRED_HINT_RE = re.compile(
    r"(?:stored in|write(?:n)? to|save(?:d)? (?:to|as)|output(?: file)?|file titled|/app/)"
    r"[^\n]{0,40}?(/app/[A-Za-z0-9_./-]+)",
    re.IGNORECASE,
)


class CheckpointDeadlineMiddleware(Middleware):
    """Nudge to publish best-so-far before the agent wall clock expires.

    Fires when ``remaining_sec`` first drops below each milestone in
    ``remaining_milestones_sec``, but only if no write to a required/candidate
    ``/app`` path has been observed yet (or ``always_nudge`` is true).
    """

    def __init__(
        self,
        *,
        agent_timeout_sec: int = 900,
        remaining_milestones_sec: list[int] | None = None,
        max_nudges: int = 3,
        always_nudge: bool = False,
    ) -> None:
        self.agent_timeout_sec = agent_timeout_sec
        milestones = remaining_milestones_sec or [360, 180, 90]
        self.remaining_milestones_sec = sorted(
            {int(m) for m in milestones if int(m) > 0}, reverse=True
        )
        self.max_nudges = max_nudges
        self.always_nudge = always_nudge

    def before_agent(self, hook_input: BeforeAgentHookInput) -> HookResult:
        state = self._load_state(hook_input.agent_state.get_global_value(STATE_KEY, {}))
        required: set[str] = set()
        for message in hook_input.messages:
            if getattr(message, "role", None) != Role.USER:
                continue
            text = self._message_text(message)
            required.update(DELIVERABLE_PATH_RE.findall(text))
            for match in REQUIRED_HINT_RE.finditer(text):
                required.add(match.group(1).rstrip(".,;)'\""))
        state["required_paths"] = required
        state["candidate_paths"] = set()
        state["started_monotonic"] = time.monotonic()
        state["nudge_count"] = 0
        state["fired_milestones"] = set()
        state["nudge_fired"] = False
        hook_input.agent_state.set_global_value(STATE_KEY, self._dump_state(state))
        if required:
            logger.info(
                "[CheckpointDeadlineMiddleware] Required paths from task: %s",
                sorted(required),
            )
        return HookResult.no_changes()

    def after_tool(self, hook_input: AfterToolHookInput) -> HookResult:
        if hook_input.tool_name != "run_shell_command":
            return HookResult.no_changes()
        tool_input = hook_input.tool_input if isinstance(hook_input.tool_input, dict) else {}
        command = str(tool_input.get("command", ""))
        state = self._load_state(hook_input.agent_state.get_global_value(STATE_KEY, {}))
        for match in DELIVERABLE_PATH_RE.finditer(command):
            path = match.group(1).rstrip(".,;)'\"")
            if WRITE_CMD_RE.search(command):
                state["candidate_paths"].add(path)
        hook_input.agent_state.set_global_value(STATE_KEY, self._dump_state(state))
        return HookResult.no_changes()

    def before_model(self, hook_input: BeforeModelHookInput) -> HookResult:
        state = self._load_state(hook_input.agent_state.get_global_value(STATE_KEY, {}))
        remaining_sec, elapsed_sec = self._remaining(state)
        if remaining_sec is None:
            return HookResult.no_changes()

        has_assistant = any(
            getattr(message, "role", None) == Role.ASSISTANT for message in hook_input.messages
        )
        if not has_assistant:
            return HookResult.no_changes()

        if state["nudge_count"] >= self.max_nudges:
            return HookResult.no_changes()

        published = bool(state["required_paths"] & state["candidate_paths"]) or (
            not state["required_paths"] and bool(state["candidate_paths"])
        )
        if published and not self.always_nudge:
            return HookResult.no_changes()

        milestone = None
        for m in self.remaining_milestones_sec:
            if remaining_sec <= m and m not in state["fired_milestones"]:
                milestone = m
                break
        if milestone is None:
            return HookResult.no_changes()

        state["fired_milestones"].add(milestone)
        state["nudge_count"] += 1
        state["nudge_fired"] = True
        hook_input.agent_state.set_global_value(STATE_KEY, self._dump_state(state))

        targets = sorted(state["required_paths"] or state["candidate_paths"]) or [
            "/app/<required>"
        ]
        paths = ", ".join(targets[:4])
        reminder = (
            f"CheckpointDeadline: ~{remaining_sec}s remain of {self.agent_timeout_sec}s "
            f"agent wall (elapsed≈{elapsed_sec}s; milestone≤{milestone}s remaining). "
            "STOP launching another long optimize/train sweep. "
            f"Immediately publish the best current candidate to: {paths}. "
            "Prefer a scored partial deliverable over AgentTimeoutError with nothing on disk. "
            "After writing, run one minimal existence check (`test -s`) and finish."
        )
        updated_messages = list(hook_input.messages)
        updated_messages.append(Message(role=Role.FRAMEWORK, content=[TextBlock(text=reminder)]))
        logger.info(
            "[CheckpointDeadlineMiddleware] Nudge #%s milestone≤%ss elapsed=%ss remaining=%ss targets=%s",
            state["nudge_count"],
            milestone,
            elapsed_sec,
            remaining_sec,
            paths,
        )
        return HookResult.with_modifications(messages=updated_messages)

    def _remaining(self, state: dict[str, Any]) -> tuple[int | None, int | None]:
        started = state.get("started_monotonic")
        if started is None:
            return None, None
        elapsed = max(0, int(time.monotonic() - float(started)))
        remaining = max(0, int(self.agent_timeout_sec) - elapsed)
        return remaining, elapsed

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

    def _load_state(self, raw_state: Any) -> dict[str, Any]:
        state = raw_state if isinstance(raw_state, dict) else {}

        def _as_set(value: Any) -> set[str]:
            if isinstance(value, set):
                return {str(v) for v in value}
            if isinstance(value, (list, tuple)):
                return {str(v) for v in value}
            return set()

        return {
            "required_paths": _as_set(state.get("required_paths")),
            "candidate_paths": _as_set(state.get("candidate_paths")),
            "started_monotonic": state.get("started_monotonic"),
            "nudge_count": int(state.get("nudge_count", 0) or 0),
            "fired_milestones": {int(x) for x in _as_set(state.get("fired_milestones"))},
            "nudge_fired": bool(state.get("nudge_fired", False)),
        }

    def _dump_state(self, state: dict[str, Any]) -> dict[str, Any]:
        return {
            "required_paths": sorted(state.get("required_paths", set())),
            "candidate_paths": sorted(state.get("candidate_paths", set())),
            "started_monotonic": state.get("started_monotonic"),
            "nudge_count": int(state.get("nudge_count", 0) or 0),
            "fired_milestones": sorted(state.get("fired_milestones", set())),
            "nudge_fired": bool(state.get("nudge_fired", False)),
        }
