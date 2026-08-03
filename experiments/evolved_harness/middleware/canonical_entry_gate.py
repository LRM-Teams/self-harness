"""Canonical public-entry gate — AHE §2 Mirror the evaluator portable point.

When deliverables are written (or a service is brought up) but acceptance was
only checked via scratch helpers, hidden build dirs, non-contract cwd, or
LD_LIBRARY_PATH / loader workarounds, soft-nudge to re-run the final check from
the task's canonical public entry / path / cwd. No domain recipe tips.
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

STATE_KEY = "canonical_entry_gate_state"
logger = logging.getLogger(__name__)

DELIVERABLE_PATH_RE = re.compile(r"(/app/[A-Za-z0-9_./-]+)")
WRITE_CMD_RE = re.compile(
    r"(?:>|tee\b|cp\b|mv\b|install\b|write_text|open\([^)]*['\"]w|"
    r"\.save\(|torch\.save|pickle\.dump|joblib\.dump|to_json|json\.dump|"
    r"systemctl\b|nginx\b|service\b)",
    re.IGNORECASE,
)
REQUIRED_HINT_RE = re.compile(
    r"(?:stored in|write(?:n)? to|save(?:d)? (?:to|as)|output(?: file)?|file titled|"
    r"JSON file called|save your solution in the file|/app/|"
    r"listen(?:ing)? on|port\s+|bind(?:s)? to|endpoint)"
    r"[^\n]{0,80}?(/app/[A-Za-z0-9_./-]+|\b\d{2,5}\b)",
    re.IGNORECASE,
)
PORT_RE = re.compile(r"\b(?:port|localhost:|127\.0\.0\.1:)\s*(\d{2,5})\b", re.IGNORECASE)
PUBLIC_BIN_RE = re.compile(
    r"(?:run|execute|invoke|call|start)\s+(?:the\s+)?"
    r"([A-Za-z0-9_./-]+\.(?:py|sh|sql|bin)|/[A-Za-z0-9_./-]+)",
    re.IGNORECASE,
)

# Non-canonical verification signals (AHE §2 debugging-only paths).
NON_CANONICAL_RE = re.compile(
    r"(?:"
    r"/tmp/|/scratch/|\bscratch[_-]|"
    r"\bLD_LIBRARY_PATH\b|\bPYTHONPATH\s*=|"
    r"\bmy_?test\b|\btmp_?check\b|\bvalidate_local\b|\bself_?check\b|"
    r"\bhelper[_-]?(?:test|check|script)\b|"
    r"/\.build/|/build/tmp/|\bhidden[_-]?build\b|"
    r"\bcd\s+/tmp\b|\bcd\s+/scratch\b"
    r")",
    re.IGNORECASE,
)

# Evidence the agent re-checked from a public / contract-facing entry.
CANONICAL_EVIDENCE_RE = re.compile(
    r"(?:"
    r"\bpytest\b|"
    r"\bpython3?\s+/app/[^\n]+\.py\b|"
    r"\bpython3?\s+[^\n]*(?:check|eval|verify|validate|test_outputs)\.py\b|"
    r"\bcurl\b\s+[^\n]*(?:localhost|127\.0\.0\.1)|"
    r"\bwget\b\s+[^\n]*(?:localhost|127\.0\.0\.1)|"
    r"\bnc\s+-z\b|\bss\s+-l|"
    r"\bsqlite3\s+/app/|"
    r"\b/app/[A-Za-z0-9_./-]+\.(?:py|sh|sql|bin)\b|"
    r"canonical\s+public\s+entry|evaluator-facing"
    r")",
    re.IGNORECASE,
)

_SIDECAR_CANDIDATES = (
    Path("/logs/agent/canonical_entry.nudge.txt"),
    Path("canonical_entry.nudge.txt"),
)


class CanonicalEntryGateMiddleware(Middleware):
    """Require final acceptance via canonical public entry/path/cwd."""

    def __init__(
        self,
        *,
        min_iterations_before_nudge: int = 3,
        renudge_every_iterations: int = 8,
        max_nudges: int = 3,
    ) -> None:
        self.min_iterations_before_nudge = int(min_iterations_before_nudge)
        self.renudge_every_iterations = int(renudge_every_iterations)
        self.max_nudges = int(max_nudges)

    def before_agent(self, hook_input: BeforeAgentHookInput) -> HookResult:
        state = self._load_state(hook_input.agent_state.get_global_value(STATE_KEY, {}))
        required: set[str] = set()
        ports: set[str] = set()
        for message in hook_input.messages:
            if getattr(message, "role", None) != Role.USER:
                continue
            text = self._message_text(message)
            required.update(DELIVERABLE_PATH_RE.findall(text))
            for match in REQUIRED_HINT_RE.finditer(text):
                token = match.group(1).rstrip(".,;)'\"")
                if token.isdigit():
                    ports.add(token)
                elif token.startswith("/app/"):
                    required.add(token)
            ports.update(PORT_RE.findall(text))
            for match in PUBLIC_BIN_RE.finditer(text):
                cand = match.group(1)
                if cand.startswith("/app/"):
                    required.add(cand)

        state["required_paths"] = required
        state["required_ports"] = ports
        state["written_paths"] = set()
        state["non_canonical_count"] = 0
        state["canonical_evidence"] = 0
        state["nudge_count"] = 0
        state["last_nudge_iteration"] = -10_000
        state["nudge_fired"] = False
        hook_input.agent_state.set_global_value(STATE_KEY, self._dump_state(state))
        if required or ports:
            logger.info(
                "[CanonicalEntryGateMiddleware] required=%s ports=%s",
                sorted(required),
                sorted(ports),
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

        if NON_CANONICAL_RE.search(command):
            state["non_canonical_count"] = int(state.get("non_canonical_count", 0) or 0) + 1
        if CANONICAL_EVIDENCE_RE.search(command):
            state["canonical_evidence"] = int(state.get("canonical_evidence", 0) or 0) + 1

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
        if (
            state["nudge_count"] > 0
            and iteration - state["last_nudge_iteration"] < self.renudge_every_iterations
        ):
            return HookResult.no_changes()

        written = bool(state["written_paths"] & state["required_paths"]) or (
            not state["required_paths"] and bool(state["written_paths"])
        )
        # Service tasks may only expose ports in the contract (no /app write yet).
        service_shaped = bool(state.get("required_ports")) and bool(state["written_paths"])
        if not written and not service_shaped:
            return HookResult.no_changes()

        canonical = int(state.get("canonical_evidence", 0) or 0)
        if canonical >= 1:
            return HookResult.no_changes()

        non_can = int(state.get("non_canonical_count", 0) or 0)
        # Nudge when non-canonical checks happened, or late in the run with
        # deliverables but still no canonical public-entry evidence.
        if non_can < 1 and iteration < max(self.min_iterations_before_nudge + 6, 10):
            return HookResult.no_changes()

        reason = (
            f"deliverable_or_service_ready_canonical_evidence=0"
            f"_non_canonical_count={non_can}"
        )
        state["nudge_count"] += 1
        state["last_nudge_iteration"] = iteration
        state["nudge_fired"] = True
        hook_input.agent_state.set_global_value(STATE_KEY, self._dump_state(state))

        paths = ", ".join(sorted(state["required_paths"] or state["written_paths"])[:4]) or (
            "/app/<required>"
        )
        ports = ", ".join(sorted(state.get("required_ports") or [])) or "n/a"
        reminder = (
            f"CanonicalEntryGate: {reason}; iteration={iteration}. "
            f"Contract path(s): {paths}. Port(s): {ports}. "
            "A check through a scratch helper, hidden build dir, non-contract cwd, "
            "or loader/`LD_LIBRARY_PATH` workaround is only a debugging signal. "
            "Before you stop, re-run the final acceptance check from the "
            "**canonical public entry / path / cwd** the evaluator will actually "
            "call (exact binary/script/layout or live service interface). "
            "Do not invent domain tips from this reminder — only public-entry verification."
        )
        self._write_sidecar(reminder)
        updated_messages: list[Message] = []
        system_patched = False
        for message in hook_input.messages:
            if not system_patched and getattr(message, "role", None) == Role.SYSTEM:
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
            "[CanonicalEntryGateMiddleware] Nudge #%s reason=%s iteration=%s "
            "non_can=%s canonical=%s system_patched=%s",
            state["nudge_count"],
            reason,
            iteration,
            non_can,
            canonical,
            system_patched,
        )
        return HookResult.with_modifications(messages=updated_messages)

    def _write_sidecar(self, text: str) -> None:
        for path in _SIDECAR_CANDIDATES:
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                with path.open("a", encoding="utf-8") as fh:
                    fh.write(text.rstrip() + "\n")
                logger.info(
                    "[CanonicalEntryGateMiddleware] Appended fire sidecar %s", path
                )
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
            "required_ports": _as_set(state.get("required_ports")),
            "written_paths": _as_set(state.get("written_paths")),
            "non_canonical_count": int(state.get("non_canonical_count", 0) or 0),
            "canonical_evidence": int(state.get("canonical_evidence", 0) or 0),
            "nudge_count": int(state.get("nudge_count", 0) or 0),
            "last_nudge_iteration": int(
                state.get("last_nudge_iteration", -10_000) or -10_000
            ),
            "nudge_fired": bool(state.get("nudge_fired", False)),
        }

    def _dump_state(self, state: dict[str, Any]) -> dict[str, Any]:
        return {
            "required_paths": sorted(state.get("required_paths", set())),
            "required_ports": sorted(state.get("required_ports", set())),
            "written_paths": sorted(state.get("written_paths", set())),
            "non_canonical_count": int(state.get("non_canonical_count", 0) or 0),
            "canonical_evidence": int(state.get("canonical_evidence", 0) or 0),
            "nudge_count": int(state.get("nudge_count", 0) or 0),
            "last_nudge_iteration": int(
                state.get("last_nudge_iteration", -10_000) or -10_000
            ),
            "nudge_fired": bool(state.get("nudge_fired", False)),
        }
