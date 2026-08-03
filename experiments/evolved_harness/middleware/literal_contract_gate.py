"""Literal-contract gate — AHE §1 (Path C / LRM-969).

Distinct from SHELVED OfficialChecker (force named pytest), SemanticNotProxy
(existence≠semantic content), and CanonicalEntry (re-run public entry). Fires
when deliverables exist or checks run, but verification commands omit literal
contract tokens (paths / filenames / CLI / terminators) and use substitutes.
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

STATE_KEY = "literal_contract_gate_state"
logger = logging.getLogger(__name__)

DELIVERABLE_PATH_RE = re.compile(r"(/app/[A-Za-z0-9_./-]+)")
QUOTED_TOKEN_RE = re.compile(r"[`'\"]([A-Za-z0-9_./+-]{3,80})[`'\"]")
CLI_HINT_RE = re.compile(
    r"(?:run|call|invoke|execute|compile with|I will (?:test|run|call))\s+"
    r"[`'\"]?([A-Za-z0-9_./+=: -]{4,100})",
    re.IGNORECASE,
)
WRITE_CMD_RE = re.compile(
    r"(?:>|tee\b|cp\b|mv\b|install\b|write_text|open\([^)]*['\"]w|"
    r"\.save\(|json\.dump|to_json)",
    re.IGNORECASE,
)
CHECK_CMD_RE = re.compile(
    r"(?:"
    r"\bpytest\b|\bpython3?\s+[^\n]*(?:test|check|eval|verify|validate)\b|"
    r"\bassert\b|\bcmp\b|\bdiff\b|\bsha256sum\b|\bmd5sum\b|"
    r"\bcurl\b|\bwget\b|\bnc\b|\btimeout\b|"
    r"\bgcc\b|\bg\+\+|\bclang\b|"
    r"test\s+-[ef]|\[[^\]]*-[ef]"
    r")",
    re.IGNORECASE,
)
SUBSTITUTE_RE = re.compile(
    r"(?:equivalent|approx(?:imate)?|similar|instead of|renamed|"
    r"proxy\s+(?:for|check)|semantic(?:ally)?\s+same|"
    r"~/|/tmp/[A-Za-z0-9_.-]+\.(?:py|sh|bin|out))",
    re.IGNORECASE,
)

_SIDECAR_CANDIDATES = (
    Path("/logs/agent/literal_contract.nudge.txt"),
    Path("literal_contract.nudge.txt"),
)

_SKIP_TOKENS = {
    "the",
    "and",
    "with",
    "from",
    "that",
    "this",
    "your",
    "file",
    "path",
    "output",
    "input",
    "python",
    "python3",
    "bash",
    "true",
    "false",
    "null",
}


class LiteralContractGateMiddleware(Middleware):
    """Nudge to copy contract literals into checks; reject substitutes."""

    def __init__(
        self,
        *,
        min_iterations_before_nudge: int = 3,
        renudge_every_iterations: int = 8,
        max_nudges: int = 3,
        min_check_events: int = 1,
    ) -> None:
        self.min_iterations_before_nudge = int(min_iterations_before_nudge)
        self.renudge_every_iterations = int(renudge_every_iterations)
        self.max_nudges = int(max_nudges)
        self.min_check_events = int(min_check_events)

    def before_agent(self, hook_input: BeforeAgentHookInput) -> HookResult:
        literals: set[str] = set()
        for message in hook_input.messages:
            if getattr(message, "role", None) != Role.USER:
                continue
            text = self._message_text(message)
            for path in DELIVERABLE_PATH_RE.findall(text):
                literals.add(path.rstrip(".,;)'\""))
            for match in QUOTED_TOKEN_RE.finditer(text):
                tok = match.group(1).strip()
                if self._is_useful_literal(tok):
                    literals.add(tok)
            for match in CLI_HINT_RE.finditer(text):
                frag = match.group(1).strip().strip("`\"'")
                # Keep first token-ish CLI head and any /app paths inside.
                for path in DELIVERABLE_PATH_RE.findall(frag):
                    literals.add(path.rstrip(".,;)'\""))
                head = frag.split()[0] if frag.split() else ""
                if self._is_useful_literal(head):
                    literals.add(head)

        state = {
            "literals": literals,
            "written": False,
            "check_events": 0,
            "literal_in_check": 0,
            "substitute_events": 0,
            "nudge_count": 0,
            "last_nudge_iteration": -10_000,
            "nudge_fired": False,
            "last_reason": "",
        }
        hook_input.agent_state.set_global_value(STATE_KEY, self._dump_state(state))
        if literals:
            logger.info(
                "[LiteralContractGateMiddleware] literals=%s",
                sorted(literals)[:12],
            )
        return HookResult.no_changes()

    def after_tool(self, hook_input: AfterToolHookInput) -> HookResult:
        if hook_input.tool_name != "run_shell_command":
            return HookResult.no_changes()
        tool_input = hook_input.tool_input if isinstance(hook_input.tool_input, dict) else {}
        command = str(tool_input.get("command", ""))
        state = self._load_state(hook_input.agent_state.get_global_value(STATE_KEY, {}))

        if WRITE_CMD_RE.search(command) and (
            DELIVERABLE_PATH_RE.search(command) or "/app/" in command
        ):
            state["written"] = True
            state["last_reason"] = "deliverable_write"

        if CHECK_CMD_RE.search(command):
            state["check_events"] = int(state["check_events"]) + 1
            state["last_reason"] = "check_without_literal"
            hit = False
            for lit in state["literals"]:
                if lit and lit in command:
                    hit = True
                    break
            if hit:
                state["literal_in_check"] = int(state["literal_in_check"]) + 1
                state["last_reason"] = "literal_seen_in_check"
            elif SUBSTITUTE_RE.search(command):
                state["substitute_events"] = int(state["substitute_events"]) + 1
                state["last_reason"] = "substitute_check"

        hook_input.agent_state.set_global_value(STATE_KEY, self._dump_state(state))
        return HookResult.no_changes()

    def before_model(self, hook_input: BeforeModelHookInput) -> HookResult:
        iteration = int(hook_input.current_iteration or 0)
        state = self._load_state(hook_input.agent_state.get_global_value(STATE_KEY, {}))
        sticky = str(state.get("sticky_reminder") or "")
        messages = list(hook_input.messages)
        sticky_applied = False

        # Tess TWEAK `2004`: sticky SYSTEM PREPEND/INSERT after first fire.
        if sticky and not self._messages_have_marker(messages, "LiteralContractGate:"):
            messages, sticky_applied = self._apply_system_marker(messages, sticky)
            if sticky_applied:
                logger.info(
                    "[LiteralContractGateMiddleware] Sticky SYSTEM re-apply "
                    "iteration=%s",
                    iteration,
                )

        def _passthrough() -> HookResult:
            if sticky_applied:
                return HookResult.with_modifications(messages=messages)
            return HookResult.no_changes()

        if iteration < self.min_iterations_before_nudge:
            return _passthrough()

        has_assistant = any(
            getattr(message, "role", None) == Role.ASSISTANT for message in messages
        )
        if not has_assistant:
            return _passthrough()

        if state["nudge_count"] >= self.max_nudges:
            return _passthrough()
        if (
            state["nudge_count"] > 0
            and iteration - state["last_nudge_iteration"] < self.renudge_every_iterations
        ):
            return _passthrough()

        # Need some work signal + missing literal-in-check evidence.
        if not state["written"] and int(state["check_events"]) < self.min_check_events:
            return _passthrough()
        if int(state["literal_in_check"]) >= 1:
            return _passthrough()
        if not state["literals"]:
            # Still nudge if checks look like substitutes without any extracted
            # literals — process-only pressure, no domain recipe.
            if int(state["check_events"]) < self.min_check_events and not state["written"]:
                return _passthrough()

        reason = state.get("last_reason") or "checks_omit_contract_literals"
        state["nudge_count"] += 1
        state["last_nudge_iteration"] = iteration
        state["nudge_fired"] = True

        sample = ", ".join(sorted(state["literals"])[:6]) or "<contract path/CLI/token>"
        reminder = (
            f"LiteralContractGate: {reason}; iteration={iteration}; "
            f"check_events={state['check_events']}; "
            f"literal_in_check={state['literal_in_check']}; "
            f"substitutes={state['substitute_events']}. "
            f"Contract literals to copy into checks: {sample}. "
            "Copy the contract's exact public path, filename, function signature, "
            "CLI syntax, literal token, or terminator into your verification "
            "commands. Equivalent substitutes do not count. Do not invent "
            "domain tips from this reminder."
        )
        self._write_sidecar(reminder)
        state["sticky_reminder"] = reminder
        hook_input.agent_state.set_global_value(STATE_KEY, self._dump_state(state))

        # Tess TWEAK `2004` (align `1751`): SYSTEM PREPEND if present else INSERT;
        # keep USER + sidecar.
        updated_messages, system_patched = self._apply_system_marker(messages, reminder)
        updated_messages.append(Message(role=Role.USER, content=[TextBlock(text=reminder)]))
        logger.info(
            "[LiteralContractGateMiddleware] Nudge #%s reason=%s iteration=%s "
            "checks=%s literal_hits=%s substitutes=%s system_patched=%s",
            state["nudge_count"],
            reason,
            iteration,
            state["check_events"],
            state["literal_in_check"],
            state["substitute_events"],
            system_patched,
        )
        return HookResult.with_modifications(messages=updated_messages)

    def _messages_have_marker(self, messages: list[Message], marker: str) -> bool:
        for message in messages:
            if self._is_system_role(message) and marker in self._message_text(message):
                return True
        return False

    def _apply_system_marker(
        self, messages: list[Message], reminder: str
    ) -> tuple[list[Message], bool]:
        """PREPEND into first SYSTEM if present; else INSERT SYSTEM at front."""
        updated_messages: list[Message] = []
        system_patched = False
        for message in messages:
            if not system_patched and self._is_system_role(message):
                existing = self._message_text(message)
                if reminder not in existing:
                    # PREPEND (Tess TWEAK): marker before existing system text.
                    patched = (reminder + "\n\n" + existing) if existing else reminder
                    updated_messages.append(
                        Message(role=Role.SYSTEM, content=[TextBlock(text=patched)])
                    )
                else:
                    updated_messages.append(message)
                system_patched = True
            else:
                updated_messages.append(message)
        if not system_patched:
            updated_messages.insert(
                0, Message(role=Role.SYSTEM, content=[TextBlock(text=reminder)])
            )
            system_patched = True
        return updated_messages, system_patched

    def _is_useful_literal(self, tok: str) -> bool:
        t = tok.strip()
        if len(t) < 3 or t.lower() in _SKIP_TOKENS:
            return False
        if t.startswith("/app/") or "/" in t or "." in t or "-" in t or "_" in t:
            return True
        return t[0].isalnum() and any(c in t for c in "./_-")

    def _write_sidecar(self, text: str) -> None:
        for path in _SIDECAR_CANDIDATES:
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                with path.open("a", encoding="utf-8") as fh:
                    fh.write(text.rstrip() + "\n")
                logger.info(
                    "[LiteralContractGateMiddleware] Appended fire sidecar %s", path
                )
                return
            except OSError:
                continue

    def _is_system_role(self, message: Any) -> bool:
        role = getattr(message, "role", None)
        if role == Role.SYSTEM:
            return True
        if isinstance(role, str) and role.lower() == "system":
            return True
        value = getattr(role, "value", None)
        return isinstance(value, str) and value.lower() == "system"

    def _message_text(self, message: Any) -> str:
        content = getattr(message, "content", None)
        if isinstance(content, str):
            return content
        get_text = getattr(message, "get_text_content", None)
        if callable(get_text):
            return get_text() or ""
        if not content:
            return ""
        parts: list[str] = []
        for block in content:
            text = getattr(block, "text", None)
            if isinstance(text, str):
                parts.append(text)
        return "\n".join(parts)

    def _dump_state(self, state: dict[str, Any]) -> dict[str, Any]:
        literals = state.get("literals") or set()
        if isinstance(literals, (list, tuple)):
            literals = set(literals)
        elif not isinstance(literals, set):
            literals = set()
        return {
            "literals": sorted(str(v) for v in literals),
            "written": bool(state.get("written", False)),
            "check_events": int(state.get("check_events", 0) or 0),
            "literal_in_check": int(state.get("literal_in_check", 0) or 0),
            "substitute_events": int(state.get("substitute_events", 0) or 0),
            "nudge_count": int(state.get("nudge_count", 0) or 0),
            "last_nudge_iteration": int(
                state.get("last_nudge_iteration", -10_000) or -10_000
            ),
            "nudge_fired": bool(state.get("nudge_fired", False)),
            "last_reason": str(state.get("last_reason") or ""),
            "sticky_reminder": str(state.get("sticky_reminder") or ""),
        }

    def _load_state(self, raw_state: Any) -> dict[str, Any]:
        state = raw_state if isinstance(raw_state, dict) else {}
        dumped = self._dump_state(state)
        dumped["literals"] = set(dumped["literals"])
        return dumped
