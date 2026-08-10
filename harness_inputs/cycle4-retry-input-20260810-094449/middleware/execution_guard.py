from __future__ import annotations

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
)
from nexau.core.messages import Message, Role, TextBlock


class ExecutionGuardMiddleware(Middleware):
    """Runtime guardrails for recurring pass@1 failure modes.

    Iteration-1 evidence showed two broad failure modes:
    - timeouts from long or repeated foreground shell work without a per-command
      budget or stop/branch decision after expensive runs.
    - normal completions that likely relied on weak local evidence instead of an
      exact public-contract check before finalizing.

    The middleware establishes a compact contract/evidence ledger after first
    inspection, bounds unbudgeted broad searches, provides sparse elapsed-time
    checkpoints, and recovers once from a final response after zero successful
    shell actions. It remains conservative for required builds and long jobs.
    """

    _LONG_SLEEP_RE = re.compile(r"(?:^|[;&|({\s])sleep\s+([1-9][0-9]{1,})(?:\s|$|[;&|)}])")
    _DESTRUCTIVE_ARTIFACT_RE = re.compile(
        r"\brm\s+(?:-[\w-]+\s+)*(?:[^;&|\n]*?)"
        r"(?:posterior|answer|result|output|out|mean|prediction|submission|mask|model|weights|"
        r"\.(?:json|csv|txt|toml|png|npy|pt|pth|bin))",
        re.IGNORECASE,
    )
    _FINAL_STATE_MUTATION_RE = re.compile(
        r"\b(?:git\s+(?:update-ref\s+-d|branch\s+-[dD]|reset\s+--hard|filter-branch|"
        r"filter-repo|reflog\s+expire|gc\b[^\n;&|]*--prune)|"
        r"find\b[^\n;&|]*\s-delete\b|truncate\s+-s\s*0\b)",
        re.IGNORECASE,
    )
    _RUNTIME_UNAVAILABLE_RE = re.compile(
        r"(ModuleNotFoundError:\s+No module named|ImportError:|command not found|python:\s+not found|"
        r"No such file or directory)",
        re.IGNORECASE,
    )
    _SYNTAX_ONLY_RE = re.compile(
        r"\b(py_compile|compileall|python3?\s+-m\s+py_compile|python3?\s+-m\s+compileall|"
        r"node\s+--check|ruby\s+-c|bash\s+-n|gcc\b[^\n;&|]*\s-fsyntax-only)\b",
        re.IGNORECASE,
    )
    _POLL_RE = re.compile(r"\b(ps|pgrep|tail|cat\s+/tmp/nexau_bash_tool_results|ls\s+-l)\b")
    _LONG_JOB_RE = re.compile(
        r"\b(Rscript|stan|sampling|train|fit|qemu|make\b|cmake\s+--build|ninja\b|cargo\s+build|"
        r"npm\s+(?:run\s+)?build)\b",
        re.IGNORECASE,
    )
    _CAP_SEARCH_RE = re.compile(
        r"\b(find\s+/(?:app|workspace|mnt|tmp)\b|grep\s+-R\b|egrep\s+-R\b|fgrep\s+-R\b|"
        r"rg\b(?![^\n;&|]*\s-(?:m|max-count)\b)|du\s+-[a-zA-Z]*[ahsx]*\s+/(?:app|workspace|mnt|tmp)\b)",
        re.IGNORECASE,
    )

    def __init__(
        self,
        *,
        long_command_ms: int = 90_000,
        long_sleep_seconds: int = 90,
        enable_final_gate: bool = True,
        progress_warning_shell_calls: int = 14,
        repeated_poll_warning_calls: int = 6,
        exploratory_timeout_ms: int = 90_000,
        enable_noop_recovery: bool = True,
        elapsed_warning_seconds: tuple[int, ...] = (900, 1800, 2700),
    ) -> None:
        self.long_command_ms = long_command_ms
        self.long_sleep_seconds = long_sleep_seconds
        self.enable_final_gate = enable_final_gate
        self.progress_warning_shell_calls = progress_warning_shell_calls
        self.repeated_poll_warning_calls = repeated_poll_warning_calls
        self.exploratory_timeout_ms = exploratory_timeout_ms
        self.enable_noop_recovery = enable_noop_recovery
        self.elapsed_warning_seconds = tuple(sorted(set(elapsed_warning_seconds)))
        self._final_gate_used: set[str] = set()
        self._noop_recovery_used: set[str] = set()
        self._ledger_prompted: set[str] = set()
        self._shell_counts: dict[str, int] = {}
        self._successful_shell_counts: dict[str, int] = {}
        self._poll_counts: dict[str, int] = {}
        self._run_started_at: dict[str, float] = {}
        self._warned: set[tuple[str, str]] = set()

    def before_agent(self, hook_input: BeforeAgentHookInput) -> HookResult:
        """Start a real wall-clock budget without changing the initial prompt."""
        run_key = self._run_key_from_state(hook_input.agent_state)
        self._run_started_at.setdefault(run_key, time.monotonic())
        return HookResult.no_changes()

    def before_model(self, hook_input: BeforeModelHookInput) -> HookResult:
        """Inject sparse delivery checkpoints only after substantial elapsed time."""
        run_key = self._run_key_from_state(hook_input.agent_state)
        started_at = self._run_started_at.setdefault(run_key, time.monotonic())
        elapsed_seconds = int(time.monotonic() - started_at)
        reached = [threshold for threshold in self.elapsed_warning_seconds if elapsed_seconds >= threshold]
        if not reached:
            return HookResult.no_changes()

        latest_threshold = reached[-1]
        if not self._mark_once(run_key, f"elapsed-{latest_threshold}"):
            return HookResult.no_changes()

        # If one long model/tool call crossed several checkpoints, suppress stale
        # lower checkpoints instead of injecting them on consecutive turns.
        self._warned.update((run_key, f"elapsed-{threshold}") for threshold in reached)
        final_threshold = self.elapsed_warning_seconds[-1] if self.elapsed_warning_seconds else latest_threshold
        if latest_threshold >= final_threshold:
            checkpoint = (
                f"CLOSING MODE: about {elapsed_seconds}s have elapsed. Do not open a new optional search, "
                "optimization, or stress-test branch. Promote the best evidence-supported candidate to the "
                "exact required path/interface, run the shortest independent contract check after the last "
                "state mutation, and finish. Required work may continue only when no viable artifact exists."
            )
        else:
            checkpoint = (
                f"DELIVERY CHECKPOINT: about {elapsed_seconds}s have elapsed. Before further exploration, "
                "place any viable candidate at the exact required path/interface. Keep optional experiments "
                "on temporary paths, treat earlier evidence as stale after later mutations, and spend the "
                "remaining budget on the largest explicit acceptance gap."
            )

        messages = list(hook_input.messages)
        messages.append(Message(role=Role.FRAMEWORK, content=[TextBlock(text=checkpoint)]))
        return HookResult.with_modifications(messages=messages)

    def after_model(self, hook_input: AfterModelHookInput) -> HookResult:
        parsed = hook_input.parsed_response
        if parsed is not None and parsed.has_calls():
            return HookResult.no_changes()

        # Do not consume the very last loop iteration; let the executor stop normally.
        if hook_input.current_iteration >= hook_input.max_iterations - 2:
            return HookResult.no_changes()

        run_key = self._run_key_from_state(hook_input.agent_state)
        if (
            self.enable_noop_recovery
            and self._shell_counts.get(run_key, 0) > 0
            and self._successful_shell_counts.get(run_key, 0) == 0
            and run_key not in self._noop_recovery_used
        ):
            self._noop_recovery_used.add(run_key)
            recovery = (
                "NO-OP RECOVERY (one-time): Every shell action so far failed, so the task cannot yet be "
                "complete. Correct the immediate execution/interface error and perform one smallest "
                "bounded action that creates or verifies the required state. Do not broaden scope or "
                "inspect hidden tests, verifier data, or reference answers."
            )
            messages = list(hook_input.messages)
            messages.append(Message(role=Role.FRAMEWORK, content=[TextBlock(text=recovery)]))
            return HookResult.with_modifications(messages=messages, force_continue=True)

        if not self.enable_final_gate:
            return HookResult.no_changes()

        if run_key in self._final_gate_used:
            return HookResult.no_changes()
        self._final_gate_used.add(run_key)

        checkpoint = (
            "FINAL-CHECKPOINT (one-time): Revisit the contract/evidence ledger derived from the user's "
            "request. Confirm each explicit public-interface, semantic, and final-state requirement with "
            "independent evidence from your own checks. Syntax, existence, self-authored expected values, "
            "or an alternate local interface are not semantic evidence when the requested interface can be "
            "tested. Required background work must be complete, and the final artifact set must be clean. "
            "Use only the user's request and your own observations; never search for hidden tests, verifier "
            "data, or reference/expected answers. If the ledger is satisfied, do not rewrite or broaden the "
            "solution—finalize now. Otherwise perform only the smallest check or fix for a specific gap."
        )
        messages = list(hook_input.messages)
        messages.append(Message(role=Role.FRAMEWORK, content=[TextBlock(text=checkpoint)]))
        return HookResult.with_modifications(messages=messages, force_continue=True)

    def before_tool(self, hook_input: BeforeToolHookInput) -> HookResult:
        """Bound only unbudgeted broad searches; preserve deliberate command budgets."""
        if hook_input.tool_name != "run_shell_command" or not isinstance(hook_input.tool_input, dict):
            return HookResult.no_changes()

        params: dict[str, Any] = dict(hook_input.tool_input)
        command = str(params.get("command", ""))
        if not command:
            return HookResult.no_changes()

        run_key = self._run_key_from_state(hook_input.agent_state)
        self._run_started_at.setdefault(run_key, time.monotonic())

        if bool(params.get("is_background", False)):
            return HookResult.no_changes()

        requested_timeout = self._as_int(params.get("timeout_ms"))
        if requested_timeout is not None:
            return HookResult.no_changes()

        if not self._CAP_SEARCH_RE.search(command):
            return HookResult.no_changes()

        params["timeout_ms"] = self.exploratory_timeout_ms
        return HookResult.with_modifications(tool_input=params)

    def after_tool(self, hook_input: AfterToolHookInput) -> HookResult:
        if hook_input.tool_name != "run_shell_command" or not isinstance(hook_input.tool_output, dict):
            return HookResult.no_changes()

        output: dict[str, Any] = dict(hook_input.tool_output)
        content = str(output.get("content", ""))
        command = ""
        if isinstance(hook_input.tool_input, dict):
            command = str(hook_input.tool_input.get("command", ""))

        run_key = self._run_key_from_state(hook_input.agent_state)
        self._shell_counts[run_key] = self._shell_counts.get(run_key, 0) + 1
        shell_count = self._shell_counts[run_key]
        exit_code = self._as_int(output.get("exit_code"))
        if not output.get("error") and exit_code in (None, 0):
            self._successful_shell_counts[run_key] = self._successful_shell_counts.get(run_key, 0) + 1

        notes: list[str] = []
        if run_key not in self._ledger_prompted:
            self._ledger_prompted.add(run_key)
            notes.append(
                "Before substantial implementation, create a concise working contract/evidence ledger "
                "from the user's request: (1) exact public invocation/interface, (2) semantic behavior and "
                "authoritative input/source constraints, and (3) required final files and forbidden extras. "
                "Also record unique source evidence that must be protected, build/data provenance, and safe "
                "margin for quantitative thresholds. For each item, name an independent check you can run. "
                "Base it only on the user's request and your own observations—never inspect hidden tests, "
                "verifier data, or reference/expected answers. If you already edited files, create the "
                "ledger now before continuing."
            )

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

        if self.progress_warning_shell_calls and shell_count >= self.progress_warning_shell_calls:
            warning_counts = {
                self.progress_warning_shell_calls,
                self.progress_warning_shell_calls * 2,
                self.progress_warning_shell_calls * 4,
            }
            if shell_count in warning_counts:
                notes.append(
                    f"You have now used {shell_count} shell commands. First promote any verified candidate "
                    "to the exact required path and recheck the final on-disk state. Then list the remaining "
                    "explicit acceptance gaps; if none remain, finalize. Otherwise choose one minimal "
                    "bounded check/fix instead of broadening scope or rerunning expensive experiments."
                )

        if self._POLL_RE.search(command):
            self._poll_counts[run_key] = self._poll_counts.get(run_key, 0) + 1
            poll_count = self._poll_counts[run_key]
            if poll_count == self.repeated_poll_warning_calls:
                notes.append(
                    "Repeated polling detected. Classify the job against the contract/evidence ledger. If it "
                    "is required, protect existing deliverables and poll only a clear completion condition; "
                    "do not substitute an incomplete fallback. If it is optional, stop it and preserve time "
                    "for final contract and artifact checks."
                )

        if "Background task started" in content and self._LONG_JOB_RE.search(command):
            notes.append(
                "Long background job started. Record its explicit completion condition in the contract/"
                "evidence ledger. If required, keep existing deliverables or write to temp files and replace "
                "them only after successful completion; if optional, stop it before it consumes the final "
                "verification budget."
            )

        if self._DESTRUCTIVE_ARTIFACT_RE.search(command):
            notes.append(
                "This command removes files that look like possible deliverables. Clean-room checks are "
                "useful, but for expensive jobs use backups/temp outputs and verify replacements exist "
                "before finalizing; never leave required final artifacts absent while waiting."
            )

        if self._FINAL_STATE_MUTATION_RE.search(command) and self._mark_once(run_key, "final-state-mutated"):
            notes.append(
                "This command destructively changes repository/file state, so earlier end-to-end evidence "
                "is stale. Do not expand into history/ref rewriting or broad cleanup unless the user "
                "explicitly required it; preserve minimal scope and rerun the exact requested interface "
                "after the last mutation before finalizing."
            )

        if self._RUNTIME_UNAVAILABLE_RE.search(content) and self._mark_once(run_key, "runtime-unavailable"):
            notes.append(
                "A required runtime/dependency may be unavailable. Do not treat syntax or import-adjacent "
                "checks as semantic proof; locate the correct environment, run a representative contract "
                "test, or keep the implementation simple and explicitly account for untested semantics."
            )

        if self._SYNTAX_ONLY_RE.search(command) and self._mark_once(run_key, "syntax-only"):
            notes.append(
                "This was a syntax-only check. Before finalizing code with behavioral requirements, run an "
                "interface/semantic test that exercises outputs, side effects, gradients/services/artifacts, "
                "or explain why such a test is impossible and avoid overclaiming."
            )

        if not notes:
            return HookResult.no_changes()

        guard_text = "\n\n[ExecutionGuard] " + " ".join(notes)
        output["content"] = content + guard_text if content else guard_text.strip()
        return HookResult.with_modifications(tool_output=output)

    def _mark_once(self, run_key: str, category: str) -> bool:
        key = (run_key, category)
        if key in self._warned:
            return False
        self._warned.add(key)
        return True

    @staticmethod
    def _run_key_from_state(agent_state: Any) -> str:
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
