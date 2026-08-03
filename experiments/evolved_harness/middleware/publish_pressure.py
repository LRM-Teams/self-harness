"""Iteration-clock publish pressure — distinct from CheckpointDeadline (SHELVED).

Fires when the agent burns iterations and/or long shell steps without publishing
required deliverables. Uses iteration + cumulative long-step ms thresholds and
long-work command signals — NOT wall-clock remaining milestones (that was
CheckpointDeadline).
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

STATE_KEY = "publish_pressure_state"
logger = logging.getLogger(__name__)

DELIVERABLE_PATH_RE = re.compile(r"(/app/[A-Za-z0-9_./-]+)")
WRITE_CMD_RE = re.compile(
    r"(?:>|tee\b|cp\b|mv\b|install\b|write_text|open\([^)]*['\"]w|"
    r"\.save\(|torch\.save|pickle\.dump|joblib\.dump)",
    re.IGNORECASE,
)
REQUIRED_HINT_RE = re.compile(
    r"(?:stored in|write(?:n)? to|save(?:d)? (?:to|as)|output(?: file)?|file titled|/app/)"
    r"[^\n]{0,40}?(/app/[A-Za-z0-9_./-]+)",
    re.IGNORECASE,
)
LONG_WORK_RE = re.compile(
    r"\b(?:train(?:ing)?|optimize|solver|epochs?|fit\(|caffe|mujoco|search\b|hyperopt|"
    r"grid.?search|bayes|fine.?tun)",
    re.IGNORECASE,
)
_SIDECAR_CANDIDATES = (
    Path("/logs/agent/publish_pressure.nudge.txt"),
    Path("publish_pressure.nudge.txt"),
)


class PublishPressureMiddleware(Middleware):
    """Press publish of best-so-far when long work burns budget without deliverables."""

    def __init__(
        self,
        *,
        long_step_ms: int = 90_000,
        long_ms_milestones: list[int] | None = None,
        iteration_milestones: list[int] | None = None,
        max_nudges: int = 3,
        min_iterations_before_nudge: int = 2,
    ) -> None:
        self.long_step_ms = int(long_step_ms)
        self.long_ms_milestones = sorted(
            {int(m) for m in (long_ms_milestones or [180_000, 360_000, 600_000]) if int(m) > 0}
        )
        self.iteration_milestones = sorted(
            {int(m) for m in (iteration_milestones or [8, 16, 24]) if int(m) > 0}
        )
        self.max_nudges = int(max_nudges)
        self.min_iterations_before_nudge = int(min_iterations_before_nudge)

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
        state["long_steps"] = 0
        state["long_ms"] = 0
        state["long_work_cmds"] = 0
        state["nudge_count"] = 0
        state["fired_long_ms"] = set()
        state["fired_iterations"] = set()
        state["nudge_fired"] = False
        hook_input.agent_state.set_global_value(STATE_KEY, self._dump_state(state))
        if required:
            logger.info(
                "[PublishPressureMiddleware] Required paths from task: %s",
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

        duration_ms = self._extract_duration_ms(hook_input.tool_output)
        if duration_ms >= self.long_step_ms or LONG_WORK_RE.search(command):
            if duration_ms >= self.long_step_ms:
                state["long_steps"] += 1
                state["long_ms"] += max(duration_ms, 0)
            if LONG_WORK_RE.search(command):
                state["long_work_cmds"] += 1

        hook_input.agent_state.set_global_value(STATE_KEY, self._dump_state(state))
        return HookResult.no_changes()

    def before_model(self, hook_input: BeforeModelHookInput) -> HookResult:
        iteration = int(hook_input.current_iteration or 0)
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

        published = bool(state["required_paths"] & state["candidate_paths"]) or (
            not state["required_paths"] and bool(state["candidate_paths"])
        )
        if published:
            return HookResult.no_changes()

        reason = None
        for milestone in self.long_ms_milestones:
            if state["long_ms"] >= milestone and milestone not in state["fired_long_ms"]:
                state["fired_long_ms"].add(milestone)
                reason = f"long_ms≥{milestone}"
                break
        if reason is None:
            for milestone in self.iteration_milestones:
                if iteration >= milestone and milestone not in state["fired_iterations"]:
                    # Only fire iteration milestones after some long work signal,
                    # so short tasks are not nagged by the iteration clock alone.
                    if state["long_steps"] > 0 or state["long_work_cmds"] > 0:
                        state["fired_iterations"].add(milestone)
                        reason = f"iteration≥{milestone}+long_work"
                        break
        if reason is None and state["long_work_cmds"] >= 2 and state["nudge_count"] == 0:
            reason = "long_work_cmds≥2"

        if reason is None:
            return HookResult.no_changes()

        state["nudge_count"] += 1
        state["nudge_fired"] = True
        hook_input.agent_state.set_global_value(STATE_KEY, self._dump_state(state))

        targets = sorted(state["required_paths"] or state["candidate_paths"]) or [
            "/app/<required>"
        ]
        paths = ", ".join(targets[:4])
        reminder = (
            f"PublishPressure: {reason}; long_steps={state['long_steps']} "
            f"long_ms≈{state['long_ms']} long_work_cmds={state['long_work_cmds']} "
            f"iteration={iteration}. "
            "You have burned long train/optimize/search budget without a complete "
            f"contract deliverable on disk ({paths}). "
            "STOP another long sweep. Immediately publish the best current candidate "
            "to the exact required path(s), run `test -s` (and a minimal evaluator if cheap), "
            "then finish. A scored partial beats AgentTimeoutError with nothing written."
        )
        self._write_sidecar(reminder)
        updated_messages = list(hook_input.messages)
        updated_messages.append(Message(role=Role.FRAMEWORK, content=[TextBlock(text=reminder)]))
        logger.info(
            "[PublishPressureMiddleware] Nudge #%s reason=%s iteration=%s long_ms=%s targets=%s",
            state["nudge_count"],
            reason,
            iteration,
            state["long_ms"],
            paths,
        )
        return HookResult.with_modifications(messages=updated_messages)

    def _write_sidecar(self, text: str) -> None:
        for path in _SIDECAR_CANDIDATES:
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                with path.open("a", encoding="utf-8") as fh:
                    fh.write(text.rstrip() + "\n")
                return
            except OSError:
                continue

    def _extract_duration_ms(self, tool_output: Any) -> int:
        if isinstance(tool_output, dict):
            value = tool_output.get("duration_ms")
            try:
                return int(value or 0)
            except (TypeError, ValueError):
                return 0
        return 0

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
            "long_steps": int(state.get("long_steps", 0) or 0),
            "long_ms": int(state.get("long_ms", 0) or 0),
            "long_work_cmds": int(state.get("long_work_cmds", 0) or 0),
            "nudge_count": int(state.get("nudge_count", 0) or 0),
            "fired_long_ms": {int(x) for x in _as_set(state.get("fired_long_ms"))},
            "fired_iterations": {int(x) for x in _as_set(state.get("fired_iterations"))},
            "nudge_fired": bool(state.get("nudge_fired", False)),
        }

    def _dump_state(self, state: dict[str, Any]) -> dict[str, Any]:
        return {
            "required_paths": sorted(state.get("required_paths", set())),
            "candidate_paths": sorted(state.get("candidate_paths", set())),
            "long_steps": int(state.get("long_steps", 0) or 0),
            "long_ms": int(state.get("long_ms", 0) or 0),
            "long_work_cmds": int(state.get("long_work_cmds", 0) or 0),
            "nudge_count": int(state.get("nudge_count", 0) or 0),
            "fired_long_ms": sorted(state.get("fired_long_ms", set())),
            "fired_iterations": sorted(state.get("fired_iterations", set())),
            "nudge_fired": bool(state.get("nudge_fired", False)),
        }
