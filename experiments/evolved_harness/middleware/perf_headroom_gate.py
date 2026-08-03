"""Perf headroom gate — AHE Mirror / performance-segment portable point.

When the task contract mentions runtime/threshold/performance/latency (or close
aliases like efficient/faster), and the agent has written a deliverable but has
not produced ≥2 timing/remeasure evidence events, soft-nudge for repeated timing
plus margin. Does NOT leak SQL/query/threshold values — only process pressure.
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

STATE_KEY = "perf_headroom_gate_state"
logger = logging.getLogger(__name__)

DELIVERABLE_PATH_RE = re.compile(r"(/app/[A-Za-z0-9_./-]+)")
WRITE_CMD_RE = re.compile(
    r"(?:>|tee\b|cp\b|mv\b|install\b|write_text|open\([^)]*['\"]w|"
    r"\.save\(|torch\.save|pickle\.dump|joblib\.dump)",
    re.IGNORECASE,
)
REQUIRED_HINT_RE = re.compile(
    r"(?:stored in|write(?:n)? to|save(?:d)? (?:to|as)|output(?: file)?|file titled|"
    r"JSON file called|save your solution in the file|/app/)"
    r"[^\n]{0,60}?(/app/[A-Za-z0-9_./-]+)",
    re.IGNORECASE,
)
# Contract surface: Tess keywords + close aliases (query-optimize uses "efficient").
PERF_CONTRACT_RE = re.compile(
    r"\b(?:runtime|threshold|performance|latency|efficient|faster|speed|"
    r"throughput|p95|timecost|pad\s*ratio)\b",
    re.IGNORECASE,
)
# Timing / remeasure evidence (process-level; not domain tips).
TIMING_EVIDENCE_RE = re.compile(
    r"(?:/usr/bin/time\b|\bhyperfine\b|\bperf\s+stat\b|\bbenchmark\b|"
    r"\btime\s+-|\bTIMEFORMAT\b|\bdate\s+\+%s\b|\bdatetime\.now\(|"
    r"\bperf_counter\b|\bmonotonic\b|\bwall.?clock\b|\belapsed\b|"
    r"\brepeat(?:ed)?\s+(?:run|trial|measure)|"
    r"\bfor\s+\w+\s+in\s+\$\(seq\b|\btrial[s]?\s*=|"
    r"\.timer\b|EXPLAIN\s+ANALYZE)",
    re.IGNORECASE,
)
EVAL_RUN_RE = re.compile(
    r"\b(?:pytest\b|python3?\s+[^\n]*(?:check|eval|test_outputs|cost_model|baseline))"
    r"|\bsqlite3\b",
    re.IGNORECASE,
)

_SIDECAR_CANDIDATES = (
    Path("/logs/agent/perf_headroom.nudge.txt"),
    Path("perf_headroom.nudge.txt"),
)


class PerfHeadroomGateMiddleware(Middleware):
    """Require repeated timing evidence + headroom on performance contracts."""

    def __init__(
        self,
        *,
        min_iterations_before_nudge: int = 3,
        renudge_every_iterations: int = 8,
        max_nudges: int = 3,
        min_timing_evidence: int = 2,
    ) -> None:
        self.min_iterations_before_nudge = int(min_iterations_before_nudge)
        self.renudge_every_iterations = int(renudge_every_iterations)
        self.max_nudges = int(max_nudges)
        self.min_timing_evidence = int(min_timing_evidence)

    def before_agent(self, hook_input: BeforeAgentHookInput) -> HookResult:
        state = self._load_state(hook_input.agent_state.get_global_value(STATE_KEY, {}))
        required: set[str] = set()
        has_perf_contract = False
        for message in hook_input.messages:
            if getattr(message, "role", None) != Role.USER:
                continue
            text = self._message_text(message)
            if PERF_CONTRACT_RE.search(text):
                has_perf_contract = True
            required.update(DELIVERABLE_PATH_RE.findall(text))
            for match in REQUIRED_HINT_RE.finditer(text):
                required.add(match.group(1).rstrip(".,;)'\""))

        state["required_paths"] = required
        state["has_perf_contract"] = has_perf_contract
        state["written_paths"] = set()
        state["timing_evidence"] = 0
        state["eval_runs"] = 0
        state["nudge_count"] = 0
        state["last_nudge_iteration"] = -10_000
        state["nudge_fired"] = False
        hook_input.agent_state.set_global_value(STATE_KEY, self._dump_state(state))
        if has_perf_contract:
            logger.info(
                "[PerfHeadroomGateMiddleware] perf_contract=Y required=%s",
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
                state["written_paths"].add(path)

        if TIMING_EVIDENCE_RE.search(command):
            state["timing_evidence"] = int(state.get("timing_evidence", 0) or 0) + 1
        if EVAL_RUN_RE.search(command):
            state["eval_runs"] = int(state.get("eval_runs", 0) or 0) + 1
            # A second+ eval/check run after a write also counts as remeasure.
            if int(state.get("eval_runs", 0) or 0) >= 2:
                state["timing_evidence"] = max(
                    int(state.get("timing_evidence", 0) or 0),
                    int(state.get("eval_runs", 0) or 0),
                )

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
        if not state.get("has_perf_contract"):
            return HookResult.no_changes()
        if state["nudge_count"] >= self.max_nudges:
            return HookResult.no_changes()
        if (
            state["nudge_count"] > 0
            and iteration - state["last_nudge_iteration"] < self.renudge_every_iterations
        ):
            return HookResult.no_changes()

        written = bool(state["written_paths"] & state["required_paths"]) or (
            not state["required_paths"] and bool(state["written_paths"])
        )
        timing = int(state.get("timing_evidence", 0) or 0)
        if not written:
            return HookResult.no_changes()
        if timing >= self.min_timing_evidence:
            return HookResult.no_changes()

        reason = f"deliverable_written_timing_evidence={timing}<{self.min_timing_evidence}"
        state["nudge_count"] += 1
        state["last_nudge_iteration"] = iteration
        state["nudge_fired"] = True
        hook_input.agent_state.set_global_value(STATE_KEY, self._dump_state(state))

        paths = ", ".join(sorted(state["required_paths"] or state["written_paths"])[:4]) or (
            "/app/<required>"
        )
        reminder = (
            f"PerfHeadroomGate: {reason}; iteration={iteration}. "
            f"Contract path(s): {paths}. "
            "Near-threshold single wins are not enough — produce **repeated** "
            "timing/remeasure evidence (≥2 independent measurements) and keep "
            "**headroom/margin** under the contract limits before you stop. "
            "Re-run your own measurement harness on the published deliverable; "
            "do not treat one lucky sample as final. "
            "Do not invent domain tips from this reminder — only remeasure + margin."
        )
        self._write_sidecar(reminder)
        # Tess TWEAK `1347`: USER-only inject fired sidecar but cleaned
        # InMemoryTracer dropped mid-run USER → `PerfHeadroomGate:` = 0.
        # Dual-land like OfficialChecker/LocalContext t2: patch SYSTEM
        # (survives as `system_prompt`) + keep USER for the live model + sidecar.
        updated_messages: list[Message] = []
        system_patched = False
        for message in hook_input.messages:
            if (
                not system_patched
                and getattr(message, "role", None) == Role.SYSTEM
            ):
                existing = ""
                get_text = getattr(message, "get_text_content", None)
                if callable(get_text):
                    existing = get_text() or ""
                else:
                    existing = self._message_text(message)
                if reminder not in existing:
                    patched = (existing + "\n\n" + reminder) if existing else reminder
                    updated_messages.append(
                        Message(role=Role.SYSTEM, content=[TextBlock(text=patched)])
                    )
                else:
                    updated_messages.append(message)
                system_patched = True
            else:
                updated_messages.append(message)
        updated_messages.append(Message(role=Role.USER, content=[TextBlock(text=reminder)]))
        logger.info(
            "[PerfHeadroomGateMiddleware] Nudge #%s reason=%s iteration=%s timing=%s "
            "system_patched=%s",
            state["nudge_count"],
            reason,
            iteration,
            timing,
            system_patched,
        )
        return HookResult.with_modifications(messages=updated_messages)

    def _write_sidecar(self, text: str) -> None:
        for path in _SIDECAR_CANDIDATES:
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                with path.open("a", encoding="utf-8") as fh:
                    fh.write(text.rstrip() + "\n")
                logger.info("[PerfHeadroomGateMiddleware] Appended fire sidecar %s", path)
                return
            except OSError:
                continue

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
            "written_paths": _as_set(state.get("written_paths")),
            "has_perf_contract": bool(state.get("has_perf_contract", False)),
            "timing_evidence": int(state.get("timing_evidence", 0) or 0),
            "eval_runs": int(state.get("eval_runs", 0) or 0),
            "nudge_count": int(state.get("nudge_count", 0) or 0),
            "last_nudge_iteration": int(state.get("last_nudge_iteration", -10_000) or -10_000),
            "nudge_fired": bool(state.get("nudge_fired", False)),
        }

    def _dump_state(self, state: dict[str, Any]) -> dict[str, Any]:
        return {
            "required_paths": sorted(state.get("required_paths", set())),
            "written_paths": sorted(state.get("written_paths", set())),
            "has_perf_contract": bool(state.get("has_perf_contract", False)),
            "timing_evidence": int(state.get("timing_evidence", 0) or 0),
            "eval_runs": int(state.get("eval_runs", 0) or 0),
            "nudge_count": int(state.get("nudge_count", 0) or 0),
            "last_nudge_iteration": int(state.get("last_nudge_iteration", -10_000) or -10_000),
            "nudge_fired": bool(state.get("nudge_fired", False)),
        }
