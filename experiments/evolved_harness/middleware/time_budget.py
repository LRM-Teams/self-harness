"""Hard wall-clock budget: force publish best candidate, then stop search."""

from __future__ import annotations

import logging
import re
import time
from typing import Any

from nexau.archs.main_sub.execution.hooks import (
    AfterModelHookInput,
    AfterToolHookInput,
    BeforeAgentHookInput,
    BeforeModelHookInput,
    BeforeToolHookInput,
    HookResult,
    Middleware,
    ModelCallFn,
    ModelCallParams,
)
from nexau.archs.main_sub.execution.parse_structures import ParsedResponse
from nexau.archs.main_sub.execution.stop_reason import AgentStopReason
from nexau.core.messages import Message, Role, TextBlock

STATE_KEY = "time_budget_state"
logger = logging.getLogger(__name__)

DELIVERABLE_PATH_RE = re.compile(r"(/app/[A-Za-z0-9_./-]+)")
WRITE_CMD_RE = re.compile(
    r"(?:>|tee\b|cp\b|mv\b|install\b|fasttext\s+supervised|\.save\(|torch\.save|"
    r"pickle\.dump|write_text|open\([^)]*['\"]w)",
    re.IGNORECASE,
)
REQUIRED_HINT_RE = re.compile(
    r"(?:stored in|write(?:n)? to|save(?:d)? (?:to|as)|output(?: file)?|file titled|/app/)"
    r"[^\n]{0,40}?(/app/[A-Za-z0-9_./-]+)",
    re.IGNORECASE,
)
# Long / exploratory work that burns the final window without publishing.
EXPENSIVE_CMD_RE = re.compile(
    r"(?:"
    r"fasttext\s+supervised|"
    r"\bepoch\s+\d{2,}|"
    r"\bwhile\b|\bfor\s+\w+\s+in\s+\{|"
    r"\bsleep\s+\d{2,}|"
    r"hyperopt|optuna|grid.?search|random.?search|"
    r"pip\s+install|apt-?get\b|"
    r"train(?:ing)?\b.*\bmodel\b"
    r")",
    re.IGNORECASE,
)
PUBLISH_CMD_RE = re.compile(
    r"(?:"
    r"\bcp\b|\bmv\b|\btee\b|fasttext\s+supervised|"
    r"test\s+-[ef]\s+/app/|ls\s+-l\s+/app/|"
    r"pytest\b|python3?\s+[^\n]{0,80}test"
    r")",
    re.IGNORECASE,
)


class TimeBudgetMiddleware(Middleware):
    """Hard cutoff: finalize/publish in the last window, then force-stop.

    Soft prompt nudges alone failed on ``train-fasttext`` (3600s AgentTimeoutError,
    unscored). This revision:

    1. FINALIZE window (``remaining <= finalize_threshold_sec``): inject a hard
       publish instruction and rewrite expensive shell commands into a publish
       smoke check.
    2. HARD STOP (``remaining <= hard_stop_sec`` or finalize turn budget used):
       strip further tool calls and set ``force_stop_reason`` so the agent exits
       before harbor ``AgentTimeoutError``, leaving artifacts for the verifier.
    """

    def __init__(
        self,
        *,
        agent_timeout_sec: int = 3600,
        finalize_threshold_sec: int = 120,
        hard_stop_sec: int = 45,
        max_finalize_model_turns: int = 3,
    ) -> None:
        self.agent_timeout_sec = agent_timeout_sec
        self.finalize_threshold_sec = finalize_threshold_sec
        self.hard_stop_sec = hard_stop_sec
        self.max_finalize_model_turns = max_finalize_model_turns

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
        state["started_monotonic"] = time.monotonic()
        state["finalize_emitted"] = False
        state["hard_stop_applied"] = False
        state["finalize_model_turns"] = 0
        hook_input.agent_state.set_global_value(STATE_KEY, self._dump_state(state))
        if required:
            logger.info(
                "[TimeBudgetMiddleware] Required paths from task: %s",
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

        if remaining_sec > self.finalize_threshold_sec:
            return HookResult.no_changes()

        if state.get("finalize_emitted"):
            state["finalize_model_turns"] = int(state.get("finalize_model_turns", 0) or 0) + 1
            hook_input.agent_state.set_global_value(STATE_KEY, self._dump_state(state))
            return HookResult.no_changes()

        state["finalize_emitted"] = True
        state["finalize_model_turns"] = 0
        hook_input.agent_state.set_global_value(STATE_KEY, self._dump_state(state))

        targets = sorted(state["required_paths"] or state["candidate_paths"]) or ["/app/<required>"]
        paths = ", ".join(targets[:4])
        reminder = (
            f"⛔ HARD TIME BUDGET — ~{remaining_sec}s remain "
            f"(finalize≤{self.finalize_threshold_sec}s, hard-stop≤{self.hard_stop_sec}s). "
            "STOP all new searches / training sweeps / long foreground jobs NOW. "
            f"Copy the best current candidate to the contract path(s): {paths}. "
            "Then run one minimal existence/evaluator-style check and finish. "
            "Further exploratory commands will be blocked by middleware. "
            "A scored attempt with a published artifact beats AgentTimeoutError."
        )
        updated_messages = list(hook_input.messages)
        updated_messages.append(Message(role=Role.FRAMEWORK, content=[TextBlock(text=reminder)]))
        logger.info(
            "[TimeBudgetMiddleware] HARD finalize at elapsed=%ss remaining=%ss targets=%s",
            elapsed_sec,
            remaining_sec,
            paths,
        )
        return HookResult.with_modifications(messages=updated_messages)

    def before_tool(self, hook_input: BeforeToolHookInput) -> HookResult:
        if hook_input.tool_name != "run_shell_command":
            return HookResult.no_changes()

        state = self._load_state(hook_input.agent_state.get_global_value(STATE_KEY, {}))
        remaining_sec, _ = self._remaining(state)
        if remaining_sec is None or remaining_sec > self.finalize_threshold_sec:
            return HookResult.no_changes()

        tool_input = dict(hook_input.tool_input) if isinstance(hook_input.tool_input, dict) else {}
        command = str(tool_input.get("command", ""))
        if not command:
            return HookResult.no_changes()

        # Allow publish / smoke checks; rewrite expensive exploration.
        if PUBLISH_CMD_RE.search(command) and not EXPENSIVE_CMD_RE.search(command):
            return HookResult.no_changes()
        if not EXPENSIVE_CMD_RE.search(command) and remaining_sec > self.hard_stop_sec:
            return HookResult.no_changes()

        targets = sorted(state["required_paths"] or state["candidate_paths"]) or ["/app/model.bin"]
        path_checks = " && ".join(f'test -s "{p}" && ls -lh "{p}"' for p in targets[:3])
        rewrite = (
            "echo '[TimeBudget HARD] Blocked expensive command in finalize window.' >&2; "
            f"echo 'blocked_cmd_preview={command[:180]!r}' >&2; "
            f"{path_checks}; "
            "echo '[TimeBudget HARD] Publish/verify only — do not resume search.'"
        )
        tool_input["command"] = rewrite
        logger.info(
            "[TimeBudgetMiddleware] Rewrote expensive tool in finalize window remaining=%ss",
            remaining_sec,
        )
        return HookResult.with_modifications(tool_input=tool_input)

    def after_model(self, hook_input: AfterModelHookInput) -> HookResult:
        state = self._load_state(hook_input.agent_state.get_global_value(STATE_KEY, {}))
        remaining_sec, _ = self._remaining(state)
        if remaining_sec is None:
            return HookResult.no_changes()

        should_hard_stop = remaining_sec <= self.hard_stop_sec or (
            bool(state.get("finalize_emitted"))
            and int(state.get("finalize_model_turns", 0) or 0) >= self.max_finalize_model_turns
        )
        if not should_hard_stop:
            return HookResult.no_changes()

        parsed = hook_input.parsed_response
        if parsed is None:
            return HookResult.no_changes()
        if not (parsed.tool_calls or parsed.sub_agent_calls or parsed.batch_agent_calls):
            state["hard_stop_applied"] = True
            hook_input.agent_state.set_global_value(STATE_KEY, self._dump_state(state))
            return HookResult.no_changes()

        stripped = ParsedResponse(
            original_response=parsed.original_response,
            tool_calls=[],
            sub_agent_calls=[],
            batch_agent_calls=[],
            is_parallel_tools=False,
            is_parallel_sub_agents=False,
            model_response=parsed.model_response,
        )
        state["hard_stop_applied"] = True
        hook_input.agent_state.set_global_value(STATE_KEY, self._dump_state(state))
        logger.info(
            "[TimeBudgetMiddleware] HARD STOP stripped tool calls remaining=%ss finalize_turns=%s",
            remaining_sec,
            state.get("finalize_model_turns"),
        )
        return HookResult.with_modifications(parsed_response=stripped)

    def wrap_model_call(self, params: ModelCallParams, call_next: ModelCallFn):
        state_raw = {}
        if params.agent_state is not None:
            state_raw = params.agent_state.get_global_value(STATE_KEY, {}) or {}
        state = self._load_state(state_raw)
        remaining_sec, _ = self._remaining(state)
        # Only hard-abort model calls after the agent already got finalize turn(s).
        # SUCCESS does not stop llm_caller; non-SUCCESS returns None and ends the loop.
        finalize_turns = int(state.get("finalize_model_turns", 0) or 0)
        if remaining_sec is not None and bool(state.get("finalize_emitted")) and (
            bool(state.get("hard_stop_applied"))
            or finalize_turns >= self.max_finalize_model_turns
            or remaining_sec <= 0
        ):
            params.force_stop_reason = AgentStopReason.MAX_ITERATIONS_REACHED
            logger.info(
                "[TimeBudgetMiddleware] wrap_model_call force_stop remaining=%ss turns=%s",
                remaining_sec,
                finalize_turns,
            )
        return call_next(params)

    def _remaining(self, state: dict[str, Any]) -> tuple[int | None, int | None]:
        started = state.get("started_monotonic")
        if not isinstance(started, (int, float)):
            return None, None
        elapsed_sec = max(int(time.monotonic() - started), 0)
        remaining_sec = max(self.agent_timeout_sec - elapsed_sec, 0)
        return remaining_sec, elapsed_sec

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
            "started_monotonic": state.get("started_monotonic"),
            "finalize_emitted": bool(state.get("finalize_emitted", False)),
            "hard_stop_applied": bool(state.get("hard_stop_applied", False)),
            "finalize_model_turns": int(state.get("finalize_model_turns", 0) or 0),
            "required_paths": _as_set(state.get("required_paths")),
            "candidate_paths": _as_set(state.get("candidate_paths")),
        }

    def _dump_state(self, state: dict[str, Any]) -> dict[str, Any]:
        return {
            "started_monotonic": state.get("started_monotonic"),
            "finalize_emitted": bool(state.get("finalize_emitted", False)),
            "hard_stop_applied": bool(state.get("hard_stop_applied", False)),
            "finalize_model_turns": int(state.get("finalize_model_turns", 0) or 0),
            "required_paths": sorted(state.get("required_paths", set())),
            "candidate_paths": sorted(state.get("candidate_paths", set())),
        }
