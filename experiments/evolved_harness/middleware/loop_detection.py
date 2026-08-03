"""Nudge when the agent repeats the same shell command / edit target."""

from __future__ import annotations

import logging
import re
from collections import Counter, deque
from typing import Any

from nexau.archs.main_sub.execution.hooks import (
    AfterToolHookInput,
    BeforeAgentHookInput,
    BeforeModelHookInput,
    HookResult,
    Middleware,
)
from nexau.core.messages import Message, Role, TextBlock

STATE_KEY = "loop_detection_state"
logger = logging.getLogger(__name__)

WHITESPACE_RE = re.compile(r"\s+")
EDIT_TARGET_RE = re.compile(
    r"(?:"
    r"(?:cat|tee|cp|mv|install)\s+[^\n]*?(/app/[A-Za-z0-9_./-]+)|"
    r"(?:>>?|tee\s+-a)\s*(/app/[A-Za-z0-9_./-]+)|"
    r"sed\s+-i[^\n]*\s(/app/[A-Za-z0-9_./-]+)|"
    r"python3?\s+-c[^\n]{0,120}open\(['\"](/app/[A-Za-z0-9_./-]+)"
    r")",
    re.IGNORECASE,
)


class LoopDetectionMiddleware(Middleware):
    """Detect repeated shell commands / edit targets and ask for a strategy change.

    Targets failure modes like ``gpt2-codegolf`` where the agent spins on the same
    generator/edit loop (e.g. Maurit pangram cycle) instead of pivoting.
    """

    def __init__(
        self,
        *,
        repeat_threshold: int = 3,
        window_size: int = 24,
        renudge_every_iterations: int = 8,
        max_nudges: int = 3,
    ) -> None:
        self.repeat_threshold = repeat_threshold
        self.window_size = window_size
        self.renudge_every_iterations = renudge_every_iterations
        self.max_nudges = max_nudges

    def before_agent(self, hook_input: BeforeAgentHookInput) -> HookResult:
        state = {
            "recent_cmds": [],
            "recent_targets": [],
            "nudge_count": 0,
            "last_nudge_iteration": -10_000,
            "last_loop_key": "",
        }
        hook_input.agent_state.set_global_value(STATE_KEY, state)
        return HookResult.no_changes()

    def after_tool(self, hook_input: AfterToolHookInput) -> HookResult:
        if hook_input.tool_name != "run_shell_command":
            return HookResult.no_changes()

        tool_input = hook_input.tool_input if isinstance(hook_input.tool_input, dict) else {}
        command = str(tool_input.get("command", ""))
        fingerprint = self._normalize_command(command)
        if not fingerprint:
            return HookResult.no_changes()

        state = self._load_state(hook_input.agent_state.get_global_value(STATE_KEY, {}))
        cmds = deque(state["recent_cmds"], maxlen=self.window_size)
        targets = deque(state["recent_targets"], maxlen=self.window_size)
        cmds.append(fingerprint)
        for match in EDIT_TARGET_RE.finditer(command):
            path = next((g for g in match.groups() if g), None)
            if path:
                targets.append(path.rstrip(".,;)'\""))
        state["recent_cmds"] = list(cmds)
        state["recent_targets"] = list(targets)
        hook_input.agent_state.set_global_value(STATE_KEY, state)
        return HookResult.no_changes()

    def before_model(self, hook_input: BeforeModelHookInput) -> HookResult:
        iteration = hook_input.current_iteration
        has_assistant = any(getattr(message, "role", None) == Role.ASSISTANT for message in hook_input.messages)
        if not has_assistant:
            return HookResult.no_changes()

        state = self._load_state(hook_input.agent_state.get_global_value(STATE_KEY, {}))
        if state["nudge_count"] >= self.max_nudges:
            return HookResult.no_changes()
        if iteration - state["last_nudge_iteration"] < self.renudge_every_iterations and state["nudge_count"] > 0:
            return HookResult.no_changes()

        loop_kind, loop_key, count = self._detect_loop(state)
        if not loop_kind:
            return HookResult.no_changes()
        if loop_key == state["last_loop_key"] and state["nudge_count"] > 0:
            # Same loop already nudged; wait for renudge window / new signature.
            if iteration - state["last_nudge_iteration"] < self.renudge_every_iterations:
                return HookResult.no_changes()

        state["nudge_count"] += 1
        state["last_nudge_iteration"] = iteration
        state["last_loop_key"] = loop_key
        hook_input.agent_state.set_global_value(STATE_KEY, state)

        preview = loop_key if len(loop_key) <= 160 else loop_key[:157] + "..."
        if loop_kind == "command":
            reminder = (
                "Loop detection: the same shell command pattern has repeated "
                f"{count}× in the recent window (`{preview}`). Stop retrying that exact approach. "
                "Change strategy: inspect failing evaluator evidence, try a different algorithm/"
                "representation, or rewrite the deliverable from a cleaner plan. Do not burn the "
                "budget on the same edit/run cycle."
            )
        else:
            reminder = (
                "Loop detection: you keep rewriting the same deliverable path "
                f"({preview}, {count}× recently) without a clear new approach. Pause and reconsider: "
                "re-read the contract and failing checks, then either pivot the method or validate a "
                "genuinely different candidate before more edits."
            )

        updated_messages = list(hook_input.messages)
        updated_messages.append(Message(role=Role.FRAMEWORK, content=[TextBlock(text=reminder)]))
        logger.info(
            "[LoopDetectionMiddleware] Nudge #%s kind=%s count=%s iteration=%s key=%s",
            state["nudge_count"],
            loop_kind,
            count,
            iteration,
            preview,
        )
        return HookResult.with_modifications(messages=updated_messages)

    def _detect_loop(self, state: dict[str, Any]) -> tuple[str, str, int]:
        cmd_counts = Counter(state.get("recent_cmds") or [])
        if cmd_counts:
            cmd, count = cmd_counts.most_common(1)[0]
            if count >= self.repeat_threshold and cmd:
                return "command", cmd, count

        target_counts = Counter(state.get("recent_targets") or [])
        if target_counts:
            target, count = target_counts.most_common(1)[0]
            if count >= self.repeat_threshold and target:
                return "target", target, count
        return "", "", 0

    def _normalize_command(self, command: str) -> str:
        text = WHITESPACE_RE.sub(" ", (command or "").strip())
        if len(text) > 240:
            text = text[:240]
        return text

    def _load_state(self, raw_state: Any) -> dict[str, Any]:
        state = raw_state if isinstance(raw_state, dict) else {}

        def _as_list(value: Any) -> list[str]:
            if isinstance(value, list):
                return [str(v) for v in value]
            if isinstance(value, tuple):
                return [str(v) for v in value]
            return []

        return {
            "recent_cmds": _as_list(state.get("recent_cmds")),
            "recent_targets": _as_list(state.get("recent_targets")),
            "nudge_count": int(state.get("nudge_count", 0) or 0),
            "last_nudge_iteration": int(state.get("last_nudge_iteration", -10_000) or -10_000),
            "last_loop_key": str(state.get("last_loop_key", "") or ""),
        }
