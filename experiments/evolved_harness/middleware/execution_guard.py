from __future__ import annotations

import re
from typing import Any

from nexau.archs.main_sub.execution.hooks import (
    AfterModelHookInput,
    AfterToolHookInput,
    HookResult,
    Middleware,
)
from nexau.core.messages import Message, Role, TextBlock


class ExecutionGuardMiddleware(Middleware):
    """Small runtime guardrails for the two dominant baseline failure modes.

    Evidence from iteration 1 showed many failures stopped after weak local smoke tests
    (or stale artifacts) while several timeouts came from long blind waits or continuing
    after success. This middleware intervenes only at those boundaries:
    - one final-answer checkpoint per run, immediately before the agent would stop;
    - compact warnings after unusually long shell commands or blind sleep polling.
    """

    _LONG_SLEEP_RE = re.compile(r"(?:^|[;&|({\s])sleep\s+([1-9][0-9]{1,})(?:\s|$|[;&|)}])")

    def __init__(
        self,
        *,
        long_command_ms: int = 90_000,
        long_sleep_seconds: int = 90,
        enable_final_gate: bool = True,
    ) -> None:
        self.long_command_ms = long_command_ms
        self.long_sleep_seconds = long_sleep_seconds
        self.enable_final_gate = enable_final_gate
        self._final_gate_used: set[str] = set()

    def after_model(self, hook_input: AfterModelHookInput) -> HookResult:
        parsed = hook_input.parsed_response
        if not self.enable_final_gate or (parsed is not None and parsed.has_calls()):
            return HookResult.no_changes()

        # Do not consume the very last loop iteration; let the executor stop normally.
        if hook_input.current_iteration >= hook_input.max_iterations - 2:
            return HookResult.no_changes()

        run_key = self._run_key(hook_input)
        if run_key in self._final_gate_used:
            return HookResult.no_changes()
        self._final_gate_used.add(run_key)

        checkpoint = (
            "FINAL-CHECKPOINT (one-time guardrail): You are about to finish with no tool calls. "
            "Before finalizing, compare the current artifact against every explicit user constraint. "
            "Pay special attention to the common failure modes: stale outputs/binaries from prior tests, "
            "testing from the wrong cwd or with a different invocation than the user/verifier will use, "
            "local smoke tests that do not exercise the end-to-end contract, heuristic guesses without "
            "evidence, and continuing to optimize after all requirements are already satisfied. "
            "If all constraints are already demonstrated by recent evidence, answer final now, briefly. "
            "Otherwise run only the smallest targeted shell check or fix needed (prefer a clean-room exact "
            "invocation, deleting/renaming old outputs first when artifacts could be stale)."
        )
        messages = list(hook_input.messages)
        messages.append(Message(role=Role.FRAMEWORK, content=[TextBlock(text=checkpoint)]))
        return HookResult.with_modifications(messages=messages, force_continue=True)

    def after_tool(self, hook_input: AfterToolHookInput) -> HookResult:
        if hook_input.tool_name != "run_shell_command" or not isinstance(hook_input.tool_output, dict):
            return HookResult.no_changes()

        output: dict[str, Any] = dict(hook_input.tool_output)
        content = str(output.get("content", ""))
        command = ""
        if isinstance(hook_input.tool_input, dict):
            command = str(hook_input.tool_input.get("command", ""))

        notes: list[str] = []
        duration_ms = self._as_int(output.get("duration_ms"))
        if duration_ms is not None and duration_ms >= self.long_command_ms:
            notes.append(
                "This shell command took a large fraction of the task budget. Reassess before another "
                "expensive attempt: if task constraints are met, finalize; otherwise use a short, "
                "milestone-based check rather than blind waiting or broad rewrites."
            )

        sleep_seconds = self._longest_sleep(command)
        if sleep_seconds >= self.long_sleep_seconds:
            notes.append(
                f"Detected a long sleep ({sleep_seconds}s). For long jobs, prefer background execution "
                "plus short polls for explicit done conditions (process exit, required artifact exists, "
                "final metric/log line appears) instead of fixed multi-minute sleeps."
            )

        if "Timeout: command timed out" in content:
            notes.append(
                "The command timed out. Do not repeat the same command unchanged; reduce scope, add a "
                "timeout/checkpoint, resume from artifacts, or switch to a simpler viable approach."
            )

        if not notes:
            return HookResult.no_changes()

        guard_text = "\n\n[ExecutionGuard] " + " ".join(notes)
        output["content"] = content + guard_text if content else guard_text.strip()
        return HookResult.with_modifications(tool_output=output)

    def _run_key(self, hook_input: AfterModelHookInput) -> str:
        agent_state = hook_input.agent_state
        for attr in ("run_id", "agent_id", "root_run_id"):
            value = getattr(agent_state, attr, None)
            if value:
                return f"{attr}:{value}"
        return str(id(agent_state))

    @staticmethod
    def _as_int(value: Any) -> int | None:
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    def _longest_sleep(self, command: str) -> int:
        longest = 0
        for match in self._LONG_SLEEP_RE.finditer(command):
            try:
                longest = max(longest, int(match.group(1)))
            except ValueError:
                continue
        return longest
