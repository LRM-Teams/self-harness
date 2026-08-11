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
    checkpoints, and conditionally recovers from incomplete delivery. It also
    protects authoritative-source requirements after a failed download and
    keeps required background jobs from being mistaken for completion.
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
    _REQUIRED_BACKGROUND_RE = re.compile(
        r"\b(?:apt-get|apt|dnf|yum|pip|uv\s+pip|Rscript|train|make|cmake\s+--build|ninja|"
        r"cargo\s+build|wget|curl)\b",
        re.IGNORECASE,
    )
    _BACKGROUND_DONE_RE = re.compile(
        r"\b(?:BUILD_DONE|TRAIN_DONE|INSTALL_DONE|DOWNLOAD_DONE|Optimization Done|completed successfully)\b",
        re.IGNORECASE,
    )
    _DOWNLOAD_RE = re.compile(r"\b(?:wget|curl)\b", re.IGNORECASE)
    _PROVENANCE_SUBSTITUTE_RE = re.compile(
        r"\b(?:synthetic|fake|dummy|generated locally|random(?:ly)? generated)\b",
        re.IGNORECASE,
    )
    _SEMANTIC_SUCCESS_RE = re.compile(
        r"(?:^|\b)(?:PASS(?:ED)?|SUCCESS(?:FUL(?:LY)?)?|SUCCEEDED|ALERT|BUILD_DONE|TRAIN_DONE|"
        r"Optimization Done|end[- ]to[- ]end.*(?:works|passed|succeeded))\b",
        re.IGNORECASE | re.MULTILINE,
    )
    _REQUIRED_PATH_RE = re.compile(
        r"(?<![A-Za-z0-9_])/(?:app|git|srv|workspace)/[A-Za-z0-9._~+/@%=-]+"
    )
    _CAP_SEARCH_RE = re.compile(
        r"\b(find\s+/(?:app|workspace|mnt|tmp)\b|grep\s+-R\b|egrep\s+-R\b|fgrep\s+-R\b|"
        r"rg\b(?![^\n;&|]*\s-(?:m|max-count)\b)|du\s+-[a-zA-Z]*[ahsx]*\s+/(?:app|workspace|mnt|tmp)\b)",
        re.IGNORECASE,
    )
    _UNCERTAIN_DELIVERY_RE = re.compile(
        r"\b(?:candidate|guess(?:ed)?|likely|looks?\s+like|appears?\s+to|uncertain|ambiguous|"
        r"approx(?:imate(?:ly)?)?|best[- ]effort|manual(?:ly)?\s+(?:read|decoded)|visual(?:ly)?)\b",
        re.IGNORECASE,
    )
    _PLACEHOLDER_RE = re.compile(
        r"\b(?:UNKNOWN|PLACEHOLDER|TODO|TBD|dummy|fake|not[- ]yet[- ]known)\b",
        re.IGNORECASE,
    )
    _OUTPUT_ARTIFACT_RE = re.compile(
        r"/app/(?:out|output|result|results|answer|answers|predictions?)\.(?:txt|json|toml|csv)|"
        r"/app/[A-Za-z0-9_.-]*(?:result|output|answer|mask|prediction)[A-Za-z0-9_.-]*",
        re.IGNORECASE,
    )
    _REDIRECT_WRITE_RE = re.compile(
        r">{1,2}\s*['\"]?(?P<path>/app/[A-Za-z0-9._~+/@%=-]+)",
        re.IGNORECASE,
    )
    _TEE_WRITE_RE = re.compile(
        r"\btee\b(?:\s+-[a-zA-Z]+)*\s+['\"]?(?P<path>/app/[A-Za-z0-9._~+/@%=-]+)",
        re.IGNORECASE,
    )
    _COPY_WRITE_RE = re.compile(
        r"\b(?:cp|mv|install)\b[^\n;&|]*\s+['\"]?(?P<path>/app/[A-Za-z0-9._~+/@%=-]+)(?:['\"]?\s*(?:$|[;&|]))",
        re.IGNORECASE,
    )
    _PYTHON_OPEN_WRITE_RE = re.compile(
        r"\bopen\(\s*['\"](?P<path>/app/[A-Za-z0-9._~+/@%=-]+)['\"]\s*,\s*['\"][wax+]",
        re.IGNORECASE,
    )
    _PYTHON_PATH_WRITE_RE = re.compile(
        r"\bPath\(\s*['\"](?P<path>/app/[A-Za-z0-9._~+/@%=-]+)['\"]\s*\)"
        r"\s*\.write_(?:text|bytes)\b",
        re.IGNORECASE,
    )
    _EXACT_INTERFACE_TASK_RE = re.compile(
        r"\b(?:git\s+clone|git@|user@|ssh|sshd|web\s+interface|VNC|QMP|HMP|websocket|"
        r"gRPC|RPC|endpoint|post-receive|deployment\s+hook|listen(?:ing)?\s+on\s+port|"
        r"server\s+(?:on|using)\s+port|curl\s+https?://|https?://[^\s`]+)\b",
        re.IGNORECASE,
    )
    _SCHEMA_CONTRACT_TASK_RE = re.compile(
        r"\b(?:proto(?:buf)?|gRPC|RPC|schema|message\s+named|field\s+names?|exact\s+structure|"
        r"exactly\s+these\s+fields|JSON\s+(?:file|object|array)|TOML\s+file)\b",
        re.IGNORECASE,
    )
    _QUANTITATIVE_TASK_RE = re.compile(
        r"(?:\b(?:at\s+least|at\s+most|minimum|maximum|less\s+than|more\s+than|between)\b\s*"
        r"[0-9]+(?:\.[0-9]+)?|(?:>=|<=|>|<)\s*[0-9]+(?:\.[0-9]+)?|"
        r"[0-9]+(?:\.[0-9]+)?\s*(?:x|times)\s+faster)",
        re.IGNORECASE,
    )
    _SOURCE_RECOVERY_TASK_RE = re.compile(
        r"\b(?:WAL|write-ahead|corrupt(?:ed|ion)?|encrypted|recover(?:y|ed)?|forensic)\b",
        re.IGNORECASE,
    )
    _SOURCE_OPEN_RE = re.compile(
        r"(?:\bsqlite3\b[^\n;&|]*\.(?:db|sqlite)\b|sqlite3\.connect\s*\()",
        re.IGNORECASE,
    )
    _COMPLEX_TASK_RE = re.compile(
        r"\b(?:recover|forensic|train|model|video|image|OCR|fit(?:ting)?|compile|build|QEMU|"
        r"database|WAL|distributed|parallel|leaderboard|gBlock|protein|checkpoint)\b",
        re.IGNORECASE,
    )

    def __init__(
        self,
        *,
        long_command_ms: int = 90_000,
        long_sleep_seconds: int = 90,
        enable_final_gate: bool = True,
        enable_delivery_gate: bool = True,
        enable_evidence_gate: bool = True,
        enable_scoped_final_gate: bool = True,
        progress_warning_shell_calls: int = 14,
        repeated_poll_warning_calls: int = 6,
        exploratory_timeout_ms: int = 90_000,
        enable_noop_recovery: bool = True,
        elapsed_warning_seconds: tuple[int, ...] = (900, 1800, 2700),
    ) -> None:
        self.long_command_ms = long_command_ms
        self.long_sleep_seconds = long_sleep_seconds
        self.enable_final_gate = enable_final_gate
        self.enable_delivery_gate = enable_delivery_gate
        self.enable_evidence_gate = enable_evidence_gate
        self.enable_scoped_final_gate = enable_scoped_final_gate
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
        self._required_paths: dict[str, set[str]] = {}
        self._handled_paths: dict[str, set[str]] = {}
        self._required_background_pids: dict[str, set[int]] = {}
        self._runtime_unavailable_runs: set[str] = set()
        self._syntax_only_runs: set[str] = set()
        self._semantic_evidence_runs: set[str] = set()
        self._download_failed_runs: set[str] = set()
        self._uncertain_output_runs: set[str] = set()
        self._user_text: dict[str, str] = {}
        self._source_protection_runs: set[str] = set()
        self._source_backup_runs: set[str] = set()
        self._complex_runs: set[str] = set()

    def before_agent(self, hook_input: BeforeAgentHookInput) -> HookResult:
        """Start a real wall-clock budget without changing the initial prompt."""
        run_key = self._run_key_from_state(hook_input.agent_state)
        self._run_started_at.setdefault(run_key, time.monotonic())
        required_paths: set[str] = set()
        user_parts: list[str] = []
        for message in hook_input.messages:
            if message.role == Role.USER:
                text = self._message_text(message)
                user_parts.append(text)
                required_paths.update(self._REQUIRED_PATH_RE.findall(text))
        if user_parts:
            user_text = "\n".join(user_parts)
            self._user_text[run_key] = user_text
            if self._SOURCE_RECOVERY_TASK_RE.search(user_text):
                self._source_protection_runs.add(run_key)
            if self._COMPLEX_TASK_RE.search(user_text):
                self._complex_runs.add(run_key)
        if required_paths:
            self._required_paths[run_key] = required_paths
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
                "optimization, or stress-test branch. Promote an evidence-supported candidate to the "
                "exact required path/interface, run the shortest independent contract check after the last "
                "state mutation, and finish. Never place UNKNOWN, placeholder, guessed, below-threshold, or "
                "source-modified content at the final path merely to satisfy existence."
            )
        else:
            checkpoint = (
                f"DELIVERY CHECKPOINT: about {elapsed_seconds}s have elapsed. Before further exploration, "
                "keep unverified candidates on temporary paths. Promote only after a contract-derived semantic "
                "check passes; treat earlier evidence as stale after later mutations, and spend the remaining "
                "budget on the largest explicit acceptance gap."
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

        missing_paths = self._required_paths.get(run_key, set()) - self._handled_paths.get(run_key, set())
        if (
            self.enable_delivery_gate
            and missing_paths
            and self._mark_once(run_key, "delivery-gate")
        ):
            recovery = (
                "DELIVERY RECOVERY (one-time): The response is about to finish, but these explicit task "
                f"paths have not appeared in any successful shell action: {', '.join(sorted(missing_paths))}. "
                "Do not merely describe the solution, but also do not create UNKNOWN, placeholder, guessed, "
                "self-confirmed, or below-threshold content just to touch a path. Keep candidates in /tmp; "
                "promote an evidence-supported artifact, verify it against the user's contract, then finish. "
                "Existing input paths only need a successful inspection; requested outputs must be created."
            )
            messages = list(hook_input.messages)
            messages.append(Message(role=Role.FRAMEWORK, content=[TextBlock(text=recovery)]))
            return HookResult.with_modifications(messages=messages, force_continue=True)

        pending_pids = self._required_background_pids.get(run_key, set())
        if (
            self.enable_delivery_gate
            and pending_pids
            and self._mark_once(run_key, "background-gate")
        ):
            recovery = (
                "BACKGROUND RECOVERY (one-time): A required install/build/download job is still recorded "
                f"as pending (pid(s): {', '.join(str(pid) for pid in sorted(pending_pids))}). Starting it "
                "was not completion. Poll a short explicit done condition, inspect its log, then create and "
                "verify the requested artifact before finishing."
            )
            messages = list(hook_input.messages)
            messages.append(Message(role=Role.FRAMEWORK, content=[TextBlock(text=recovery)]))
            return HookResult.with_modifications(messages=messages, force_continue=True)

        if (
            self.enable_evidence_gate
            and run_key in self._runtime_unavailable_runs
            and run_key in self._syntax_only_runs
            and run_key not in self._semantic_evidence_runs
            and self._mark_once(run_key, "evidence-gate")
        ):
            recovery = (
                "SEMANTIC-EVIDENCE RECOVERY (one-time): A required runtime was unavailable and the only "
                "positive evidence recorded was syntax/interface inspection. Before finishing, make one "
                "bounded attempt to locate or install the real runtime and execute the smallest behavioral "
                "output/gradient/service check. If that is genuinely impossible, simplify high-risk "
                "untested logic instead of adding compatibility branches, and state the remaining gap."
            )
            messages = list(hook_input.messages)
            messages.append(Message(role=Role.FRAMEWORK, content=[TextBlock(text=recovery)]))
            return HookResult.with_modifications(messages=messages, force_continue=True)

        if run_key in self._uncertain_output_runs and self._mark_once(run_key, "uncertain-output-gate"):
            recovery = (
                "UNCERTAIN-OUTPUT RECOVERY (one-time): The current output artifact was written from a "
                "candidate/guess/approximate method. Preserve the current artifact until stronger evidence exists. "
                "Run one genuinely independent confirmation: it must use a different source, measurement family, "
                "or contract oracle—not merely a different window, sampling, rendering of the same ambiguous data, "
                "or a client generated from the implementation being tested. Replace the artifact only when the "
                "new evidence is authoritative and explains the discrepancy; otherwise return to source evidence "
                "instead of repeatedly re-fitting or self-confirming."
            )
            messages = list(hook_input.messages)
            messages.append(Message(role=Role.FRAMEWORK, content=[TextBlock(text=recovery)]))
            return HookResult.with_modifications(messages=messages, force_continue=True)

        scoped_recovery = self._scoped_final_recovery(run_key)
        if self.enable_scoped_final_gate and scoped_recovery and self._mark_once(run_key, "scoped-final-gate"):
            messages = list(hook_input.messages)
            messages.append(Message(role=Role.FRAMEWORK, content=[TextBlock(text=scoped_recovery)]))
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

        write_paths = self._artifact_write_paths(command)
        required_paths = self._required_paths.get(run_key, set())
        final_write_paths = write_paths & required_paths
        if final_write_paths and self._PLACEHOLDER_RE.search(command):
            blocked_paths = ", ".join(sorted(final_write_paths))
            params["command"] = (
                "printf '%s\\n' 'ExecutionGuard blocked placeholder content for required final path(s): "
                + blocked_paths.replace("'", "")
                + ". Write the unverified candidate under /tmp and promote it only after semantic validation.' >&2; "
                "exit 64"
            )
            return HookResult.with_modifications(tool_input=params)

        if (
            run_key in self._source_protection_runs
            and run_key not in self._source_backup_runs
            and self._SOURCE_OPEN_RE.search(command)
        ):
            backup_prefix = (
                "ahe_source_backup_dir=$(mktemp -d /tmp/ahe-source-backup.XXXXXX)\n"
                "find /app -maxdepth 1 -type f \\( -name '*.db' -o -name '*.db-*' "
                "-o -name '*.sqlite' -o -name '*.sqlite-*' -o -iname '*wal*' \\) "
                "-exec cp -a -t \"$ahe_source_backup_dir\" -- {} +\n"
                "printf 'AHE_SOURCE_BACKUP_OK=%s\\n' \"$ahe_source_backup_dir\"\n"
            )
            params["command"] = backup_prefix + command
            return HookResult.with_modifications(tool_input=params)

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
        successful = not output.get("error") and exit_code in (None, 0)
        if successful:
            self._successful_shell_counts[run_key] = self._successful_shell_counts.get(run_key, 0) + 1
            command_paths = set(self._REQUIRED_PATH_RE.findall(command))
            if command_paths:
                self._handled_paths.setdefault(run_key, set()).update(command_paths)
        if successful and "AHE_SOURCE_BACKUP_OK=" in content:
            self._source_backup_runs.add(run_key)

        if self._RUNTIME_UNAVAILABLE_RE.search(content):
            self._runtime_unavailable_runs.add(run_key)
        if self._SYNTAX_ONLY_RE.search(command):
            self._syntax_only_runs.add(run_key)
        semantic_success = bool(successful and self._SEMANTIC_SUCCESS_RE.search(content))
        if semantic_success:
            self._semantic_evidence_runs.add(run_key)

        timed_out = "Timeout: command timed out" in content
        if timed_out and self._DOWNLOAD_RE.search(command):
            self._download_failed_runs.add(run_key)

        written_paths = self._artifact_write_paths(command)
        required_paths = self._required_paths.get(run_key, set())
        final_like_write = bool(
            written_paths & required_paths
            or any(self._OUTPUT_ARTIFACT_RE.search(path) for path in written_paths)
        )
        uncertain_delivery = bool(
            successful
            and final_like_write
            and self._UNCERTAIN_DELIVERY_RE.search(command)
        )
        if uncertain_delivery:
            self._uncertain_output_runs.add(run_key)

        if "Background task started" in content and self._REQUIRED_BACKGROUND_RE.search(command):
            pids = output.get("backgroundPids")
            if isinstance(pids, list):
                pending = self._required_background_pids.setdefault(run_key, set())
                pending.update(pid for pid in (self._as_int(value) for value in pids) if pid is not None)
        if self._BACKGROUND_DONE_RE.search(content):
            self._required_background_pids.pop(run_key, None)

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

        if timed_out:
            notes.append(
                "The command timed out. Do not repeat the same command unchanged; reduce scope, add a "
                "timeout/checkpoint, resume from artifacts, or switch to a simpler viable approach."
            )
            if self._DOWNLOAD_RE.search(command):
                notes.append(
                    "This was a download timeout. Preserve the partial file and resume the same authoritative "
                    "source with `wget -c` or `curl -C -`; redirect transfer progress to a log. An alternate "
                    "mirror is acceptable only when its provenance and content can be verified. Never replace "
                    "a required real dataset, model, archive, or source tree with synthetic/fake data."
                )

        if run_key in self._download_failed_runs and self._PROVENANCE_SUBSTITUTE_RE.search(command):
            notes.append(
                "PROVENANCE STOP: A prior authoritative download failed and this command appears to create "
                "a synthetic/fake substitute. That cannot satisfy a requirement for the named real dataset, "
                "model, archive, or source. Preserve any valid artifacts, resume or use a verifiable mirror, "
                "and do not report metrics from substitute data as task evidence."
            )

        if uncertain_delivery:
            notes.append(
                "UNCERTAIN OUTPUT: This successful command actually wrote a required/final-looking artifact "
                "while describing it as a candidate/guess/approximation. Preserve it until a genuinely independent "
                "source or contract oracle confirms a replacement; a different view/window of the same ambiguous "
                "evidence or a self-generated test is not independent."
            )

        missing_paths = self._required_paths.get(run_key, set()) - self._handled_paths.get(run_key, set())
        if semantic_success and missing_paths and self._mark_once(run_key, "candidate-not-delivered"):
            notes.append(
                "A behavioral success signal was observed, but explicit task path(s) remain untouched: "
                f"{', '.join(sorted(missing_paths))}. Immediately promote the verified candidate to the exact "
                "required path before any optional experiment, then rerun the shortest check against that "
                "on-disk path."
            )

        progress_threshold = self.progress_warning_shell_calls
        if run_key in self._complex_runs:
            progress_threshold *= 2
        if progress_threshold and shell_count >= progress_threshold:
            warning_counts = {
                progress_threshold,
                progress_threshold * 2,
                progress_threshold * 4,
            }
            if shell_count in warning_counts:
                notes.append(
                    f"You have now used {shell_count} shell commands. List the remaining explicit acceptance "
                    "gaps and choose one minimal bounded check/fix. Keep unverified or below-threshold candidates "
                    "under /tmp; promote only a semantically validated artifact. If no gap remains, recheck the "
                    "exact final state and finish instead of broadening scope."
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

        if "Background task started" in content and self._REQUIRED_BACKGROUND_RE.search(command):
            notes.append(
                "Required background job started. Starting it is not task completion: the next action must be "
                "a short poll of its explicit done condition and log. Continue until the job exits successfully, "
                "then create and verify the requested artifact; do not finalize while it is only running."
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

    def _needs_scoped_final_check(self, run_key: str) -> bool:
        user_text = self._user_text.get(run_key, "")
        if not user_text:
            return False
        return bool(
            self._EXACT_INTERFACE_TASK_RE.search(user_text)
            or self._SCHEMA_CONTRACT_TASK_RE.search(user_text)
            or self._QUANTITATIVE_TASK_RE.search(user_text)
        )

    def _scoped_final_recovery(self, run_key: str) -> str:
        user_text = self._user_text.get(run_key, "")
        if not user_text or not self._needs_scoped_final_check(run_key):
            return ""
        checks: list[str] = []
        if self._EXACT_INTERFACE_TASK_RE.search(user_text):
            checks.append(
                "Exercise the exact client-facing command/URL/host/port after the last mutation. For a web UI "
                "or proxy, prove the browser/WebSocket/backend path end to end; separate HTTP 200 and backend "
                "socket checks are insufficient. For Git/SSH/deployment, use the requested remote form."
            )
        if self._SCHEMA_CONTRACT_TASK_RE.search(user_text):
            checks.append(
                "Copy every requested method, message, field, key, type, and filename directly from the user "
                "contract, then test with an independently written client/parser; generated code from your own "
                "schema cannot prove that the schema names are correct."
            )
        if self._QUANTITATIVE_TASK_RE.search(user_text):
            checks.append(
                "State the required numeric threshold beside the observed final metric. Do not finalize below "
                "the threshold, and keep a practical safety margin for timing or stochastic measurements."
            )
        return "SCOPED CONTRACT FINAL CHECK (one-time): " + " ".join(checks)

    @classmethod
    def _artifact_write_paths(cls, command: str) -> set[str]:
        paths: set[str] = set()
        for pattern in (
            cls._REDIRECT_WRITE_RE,
            cls._TEE_WRITE_RE,
            cls._COPY_WRITE_RE,
            cls._PYTHON_OPEN_WRITE_RE,
            cls._PYTHON_PATH_WRITE_RE,
        ):
            paths.update(match.group("path") for match in pattern.finditer(command))
        return paths

    def _mark_once(self, run_key: str, category: str) -> bool:
        key = (run_key, category)
        if key in self._warned:
            return False
        self._warned.add(key)
        return True

    @staticmethod
    def _message_text(message: Message) -> str:
        content = message.content
        if isinstance(content, str):
            return content
        parts: list[str] = []
        for block in content:
            text = getattr(block, "text", None)
            if isinstance(text, str):
                parts.append(text)
        return "\n".join(parts)

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
