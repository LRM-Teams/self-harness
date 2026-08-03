"""Remind agents to publish and smoke-check deliverable paths before finishing."""

from __future__ import annotations

import logging
import re
from typing import Any

from nexau.archs.main_sub.execution.hooks import (
    AfterToolHookInput,
    BeforeAgentHookInput,
    BeforeModelHookInput,
    HookResult,
    Middleware,
)
from nexau.core.messages import Message, Role, TextBlock

STATE_KEY = "artifact_publish_guard_state"
logger = logging.getLogger(__name__)

DELIVERABLE_PATH_RE = re.compile(r"(/app/[A-Za-z0-9_./-]+)")
WRITE_CMD_RE = re.compile(
    r"(?:>|tee\b|cp\b|mv\b|install\b|fasttext\s+supervised|\.save\(|torch\.save|pickle\.dump|write_text|open\([^)]*['\"]w)",
    re.IGNORECASE,
)
EXISTENCE_CHECK_RE = re.compile(r"\b(?:test\s+-[ef]|stat\b|ls\b|pytest\b|python\b.*test_)\b", re.IGNORECASE)
# Paths that look like required contract outputs in the task prompt.
REQUIRED_HINT_RE = re.compile(
    r"(?:stored in|write(?:n)? to|save(?:d)? (?:to|as)|output(?: file)?|file titled|/app/)"
    r"[^\n]{0,40}?(/app/[A-Za-z0-9_./-]+)",
    re.IGNORECASE,
)


class ArtifactPublishGuardMiddleware(Middleware):
    """Track required/candidate deliverable paths and nudge publish + smoke checks.

    Targets failure modes like ``gcode-to-text`` where the agent times out without
    writing ``/app/out.txt``, or writes a candidate but skips evaluator-style checks.
    """

    def __init__(
        self,
        *,
        min_iterations_before_nudge: int = 4,
        renudge_every_iterations: int = 12,
        max_nudges: int = 3,
    ) -> None:
        self.min_iterations_before_nudge = min_iterations_before_nudge
        self.renudge_every_iterations = renudge_every_iterations
        self.max_nudges = max_nudges

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
        state["nudge_count"] = 0
        state["last_nudge_iteration"] = -10_000
        hook_input.agent_state.set_global_value(STATE_KEY, self._dump_state(state))
        if required:
            logger.info("[ArtifactPublishGuardMiddleware] Required paths from task: %s", sorted(required))
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
            if EXISTENCE_CHECK_RE.search(command):
                state["checked_paths"].add(path)

        hook_input.agent_state.set_global_value(STATE_KEY, self._dump_state(state))
        return HookResult.no_changes()

    def before_model(self, hook_input: BeforeModelHookInput) -> HookResult:
        iteration = hook_input.current_iteration
        if iteration < self.min_iterations_before_nudge:
            return HookResult.no_changes()

        has_assistant = any(getattr(message, "role", None) == Role.ASSISTANT for message in hook_input.messages)
        if not has_assistant:
            return HookResult.no_changes()

        state = self._load_state(hook_input.agent_state.get_global_value(STATE_KEY, {}))
        if state["nudge_count"] >= self.max_nudges:
            return HookResult.no_changes()
        if iteration - state["last_nudge_iteration"] < self.renudge_every_iterations and state["nudge_count"] > 0:
            return HookResult.no_changes()

        unchecked_required = sorted(state["required_paths"] - state["checked_paths"])
        unchecked_candidates = sorted(state["candidate_paths"] - state["checked_paths"])
        unchecked = unchecked_required or unchecked_candidates
        if not unchecked:
            return HookResult.no_changes()

        state["nudge_count"] += 1
        state["last_nudge_iteration"] = iteration
        hook_input.agent_state.set_global_value(STATE_KEY, self._dump_state(state))

        paths = ", ".join(unchecked[:4])
        if unchecked_required:
            reminder = (
                "Deliverable publish guard: task contract requires "
                f"{paths}, but those paths are still unverified on disk. "
                "Stop open-ended exploration long enough to write the final artifact to the exact "
                "required path, then smoke-check with `test -f` + evaluator-style runner "
                "(`pytest -q` or the task's named checker). A scored attempt beats timeout with no file."
            )
        else:
            reminder = (
                "Deliverable smoke check: you touched likely output path(s) "
                f"({paths}) but have not run an evaluator-style existence/content check yet. "
                "Confirm each required artifact exists at the exact path from disk "
                "(e.g. `test -f` then `pytest -q` or the task runner), not just from build logs."
            )

        updated_messages = list(hook_input.messages)
        updated_messages.append(Message(role=Role.FRAMEWORK, content=[TextBlock(text=reminder)]))
        logger.info(
            "[ArtifactPublishGuardMiddleware] Nudge #%s for %s at iteration=%s",
            state["nudge_count"],
            paths,
            iteration,
        )
        return HookResult.with_modifications(messages=updated_messages)

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
            "checked_paths": _as_set(state.get("checked_paths")),
            "nudge_count": int(state.get("nudge_count", 0) or 0),
            "last_nudge_iteration": int(state.get("last_nudge_iteration", -10_000) or -10_000),
        }

    def _dump_state(self, state: dict[str, Any]) -> dict[str, Any]:
        return {
            "required_paths": sorted(state.get("required_paths", set())),
            "candidate_paths": sorted(state.get("candidate_paths", set())),
            "checked_paths": sorted(state.get("checked_paths", set())),
            "nudge_count": int(state.get("nudge_count", 0) or 0),
            "last_nudge_iteration": int(state.get("last_nudge_iteration", -10_000) or -10_000),
        }
