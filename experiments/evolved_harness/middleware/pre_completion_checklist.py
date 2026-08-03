"""Block weak self-checks before finish — require evaluator-style verification."""

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

STATE_KEY = "pre_completion_checklist_state"
logger = logging.getLogger(__name__)

# Strong runners only suppress the nudge. Local `cmp`/`diff` in /tmp (path-tracing
# `2313`) printed PASS and falsely set evaluator_pass_seen → nudge N; treat those
# as weak proxy checks, not acceptance.
STRONG_EVALUATOR_RUN_RE = re.compile(
    r"\b(?:pytest(?:\s|$)|python\s+-m\s+pytest|uv\s+run\s+pytest|"
    r"cargo\s+test|go\s+test|npm\s+test|make\s+test|"
    r"Rscript\b)\b",
    re.IGNORECASE,
)
WEAK_ONLY_RE = re.compile(
    r"\b(?:py_compile|compileall|gcc\b|g\+\+|clang\b|rustc\b|"
    r"test\s+-[efsd]|ls\b|stat\b|wc\s+-c|file\b|which\b|"
    r"cmp\b|diff\b)\b",
    re.IGNORECASE,
)
WRITE_OR_BUILD_RE = re.compile(
    r"(?:>>?|tee\b|install\b|cp\b|mv\b|cat\s*>|\bgcc\b|\bg\+\+|\bclang\b|\brustc\b|"
    r"\bmake\b|\bcargo\s+build|\bnpm\s+run\s+build)",
    re.IGNORECASE,
)
PASS_SIGNAL_RE = re.compile(
    r"\b(?:passed|PASSED|ok\b|ALL TESTS PASSED|reward\s*=\s*1|"
    r"\d+\s+passed|\b0\s+failed)\b",
    re.IGNORECASE,
)
FAIL_SIGNAL_RE = re.compile(
    r"\b(?:FAILED|failed|AssertionError|Error:|Traceback|exit_code[=:]?\s*[1-9])\b",
    re.IGNORECASE,
)


class PreCompletionChecklistMiddleware(Middleware):
    """Nudge when the agent is about to finish without evaluator-style checks.

    Targets weak self-check finishes (compile / file-exists / short smoke) that
    miss the task's real pytest or exact verifier command — Tess 2026-07-29
    gpt2-style failure mode.
    """

    def __init__(
        self,
        *,
        min_iterations_before_nudge: int = 4,
        renudge_every_iterations: int = 10,
        max_nudges: int = 3,
    ) -> None:
        self.min_iterations_before_nudge = min_iterations_before_nudge
        self.renudge_every_iterations = renudge_every_iterations
        self.max_nudges = max_nudges

    def before_agent(self, hook_input: BeforeAgentHookInput) -> HookResult:
        state = {
            "wrote_or_built": False,
            "weak_check_seen": False,
            "evaluator_run_seen": False,
            "evaluator_pass_seen": False,
            "nudge_count": 0,
            "last_nudge_iteration": -10_000,
        }
        hook_input.agent_state.set_global_value(STATE_KEY, state)
        return HookResult.no_changes()

    def after_tool(self, hook_input: AfterToolHookInput) -> HookResult:
        if hook_input.tool_name != "run_shell_command":
            return HookResult.no_changes()

        tool_input = hook_input.tool_input if isinstance(hook_input.tool_input, dict) else {}
        command = str(tool_input.get("command", ""))
        output = self._tool_output_text(hook_input)
        state = self._load_state(hook_input.agent_state.get_global_value(STATE_KEY, {}))

        if WRITE_OR_BUILD_RE.search(command):
            state["wrote_or_built"] = True
        if WEAK_ONLY_RE.search(command):
            state["weak_check_seen"] = True
        if STRONG_EVALUATOR_RUN_RE.search(command):
            state["evaluator_run_seen"] = True
            if PASS_SIGNAL_RE.search(output) and not FAIL_SIGNAL_RE.search(output):
                state["evaluator_pass_seen"] = True
            elif PASS_SIGNAL_RE.search(output) and "passed" in output.lower() and "failed" not in output.lower():
                state["evaluator_pass_seen"] = True

        hook_input.agent_state.set_global_value(STATE_KEY, state)
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

        # Only intervene after real work, and when strong evaluator evidence is missing.
        if not state["wrote_or_built"] and not state["weak_check_seen"]:
            return HookResult.no_changes()
        if state["evaluator_pass_seen"]:
            return HookResult.no_changes()
        if state["evaluator_run_seen"] and not state["weak_check_seen"]:
            # Ran pytest/etc already; don't nag solely for missing pass signal yet.
            return HookResult.no_changes()

        state["nudge_count"] += 1
        state["last_nudge_iteration"] = iteration
        hook_input.agent_state.set_global_value(STATE_KEY, state)

        reminder = (
            "PreCompletion checklist: do not finish on weak self-checks alone "
            "(compile / `test -f` / file size / short smoke). Before the final answer, run the "
            "evaluator-style check the task actually uses — normally `pytest -q` on the exposed "
            "`test_*.py` / `test_outputs.py`, or the exact command/assert from the instruction — "
            "and require explicit pass/fail output. If it fails, fix against that evidence; if tests "
            "are absent, mirror the stated acceptance contract literally (exact paths, stdout tokens, "
            "layouts). Compile-success or file-exists is not acceptance."
        )

        updated_messages = list(hook_input.messages)
        updated_messages.append(Message(role=Role.FRAMEWORK, content=[TextBlock(text=reminder)]))
        logger.info(
            "[PreCompletionChecklistMiddleware] Nudge #%s iteration=%s wrote=%s weak=%s eval_run=%s",
            state["nudge_count"],
            iteration,
            state["wrote_or_built"],
            state["weak_check_seen"],
            state["evaluator_run_seen"],
        )
        return HookResult.with_modifications(messages=updated_messages)

    def _tool_output_text(self, hook_input: AfterToolHookInput) -> str:
        for attr in ("tool_output", "tool_result", "result", "output"):
            value = getattr(hook_input, attr, None)
            if value is None:
                continue
            if isinstance(value, str):
                return value
            text = getattr(value, "output", None) or getattr(value, "content", None)
            if isinstance(text, str):
                return text
            return str(value)
        return ""

    def _load_state(self, raw_state: Any) -> dict[str, Any]:
        state = raw_state if isinstance(raw_state, dict) else {}
        return {
            "wrote_or_built": bool(state.get("wrote_or_built", False)),
            "weak_check_seen": bool(state.get("weak_check_seen", False)),
            "evaluator_run_seen": bool(state.get("evaluator_run_seen", False)),
            "evaluator_pass_seen": bool(state.get("evaluator_pass_seen", False)),
            "nudge_count": int(state.get("nudge_count", 0) or 0),
            "last_nudge_iteration": int(state.get("last_nudge_iteration", -10_000) or -10_000),
        }
