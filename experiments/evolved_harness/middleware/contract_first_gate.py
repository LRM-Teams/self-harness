"""Contract-first gate — AHE §1 (Path C / LRM-1006).

Distinct from SHELVED LiteralContract (end-state token copy),
OfficialChecker (run official checker mid-loop), FailFast,
CanonicalEntry, SemanticNotProxy, PreserveSemantics, ShortProbe,
PersistBest.

Fires when the agent starts writing/implementing deliverables before
reading tests/verifier/instruction contract surfaces, or treats proxy
signals (exists/size/import) as acceptance — process pressure only,
never domain tips or fixture answers.
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

STATE_KEY = "contract_first_gate_state"
MARKER = "ContractFirstGate:"
logger = logging.getLogger(__name__)

# Reading acceptance contract surfaces (tests / verifier / instruction).
CONTRACT_READ_RE = re.compile(
    r"(?:"
    r"\b(?:cat|less|more|head|tail|sed\s+-n|rg|grep|awk|nl)\b[^\n]*"
    r"(?:test_outputs?\.py|test_[A-Za-z0-9_]+\.py|/tests/|tests/|"
    r"instruction\.md|task\.yaml|Dockerfile|run-tests\.sh|"
    r"verifier|pytest)"
    r"|"
    r"\bls\b[^\n]*(?:/tests\b|tests/)"
    r"|"
    r"\bpytest\b[^\n]*(?:--collect-only|-q\s+--collect)"
    r")",
    re.IGNORECASE,
)
# Writing / implementing deliverables under /app (or common outputs).
IMPLEMENT_WRITE_RE = re.compile(
    r"(?:"
    r"(?:>>?|tee\b)\s*/app/|"
    r"\b(?:cp|mv|install|rsync)\b[^\n]+/app/|"
    r"cat\s*>\s*/app/|"
    r"\bpython3?\s+[^\n]*\bopen\([^)]*['\"]/app/|"
    r"\bgcc\b|\bg\+\+|\bclang\b|\brustc\b|\bmake\b|\bcargo\s+build|"
    r"\bnpm\s+(?:init|install|run)\b|"
    r"\bprintf\b[^\n]*>\s*/app/"
    r")",
    re.IGNORECASE,
)
# Proxy "acceptance" that is not the real contract.
PROXY_ACCEPT_RE = re.compile(
    r"(?:"
    r"\btest\s+-[ef]\s+/app/|"
    r"\b\[+\s+-[ef]\s+/app/|"
    r"\bls\s+-l\s+/app/[^\n]*&&|"
    r"\bwc\s+-c\s+/app/|"
    r"\bstat\s+/app/|"
    r"\bpython3?\s+-c\s+['\"][^'\"]*\bimport\b[^'\"]*['\"]|"
    r"\bpython3?\s+[^\n]*test_outputs\.py\b(?![^\n]*pytest)"
    r")",
    re.IGNORECASE,
)

_SIDECAR_CANDIDATES = (
    Path("/logs/agent/contract_first.nudge.txt"),
    Path("contract_first.nudge.txt"),
)


class ContractFirstGateMiddleware(Middleware):
    """Nudge when implementation starts before contract read, or proxy checks replace acceptance."""

    def __init__(
        self,
        *,
        min_iterations_before_nudge: int = 2,
        renudge_every_iterations: int = 8,
        max_nudges: int = 3,
        min_risk_events: int = 1,
    ) -> None:
        self.min_iterations_before_nudge = int(min_iterations_before_nudge)
        self.renudge_every_iterations = int(renudge_every_iterations)
        self.max_nudges = int(max_nudges)
        self.min_risk_events = int(min_risk_events)

    def before_agent(self, hook_input: BeforeAgentHookInput) -> HookResult:
        state = {
            "contract_read_events": 0,
            "implement_before_contract": 0,
            "proxy_accept_events": 0,
            "implement_events": 0,
            "nudge_count": 0,
            "last_nudge_iteration": -10_000,
            "nudge_fired": False,
            "last_reason": "",
            "sticky_reminder": "",
        }
        hook_input.agent_state.set_global_value(STATE_KEY, self._dump_state(state))
        return HookResult.no_changes()

    def after_tool(self, hook_input: AfterToolHookInput) -> HookResult:
        if hook_input.tool_name != "run_shell_command":
            return HookResult.no_changes()

        tool_input = hook_input.tool_input if isinstance(hook_input.tool_input, dict) else {}
        command = str(tool_input.get("command", ""))
        state = self._load_state(hook_input.agent_state.get_global_value(STATE_KEY, {}))

        if CONTRACT_READ_RE.search(command):
            state["contract_read_events"] = int(state["contract_read_events"]) + 1
            state["last_reason"] = "contract_read_ok"

        if IMPLEMENT_WRITE_RE.search(command):
            state["implement_events"] = int(state["implement_events"]) + 1
            if int(state["contract_read_events"]) <= 0:
                state["implement_before_contract"] = (
                    int(state["implement_before_contract"]) + 1
                )
                state["last_reason"] = "implement_before_contract_read"
            else:
                state["last_reason"] = "implement_after_contract_read"

        if PROXY_ACCEPT_RE.search(command):
            state["proxy_accept_events"] = int(state["proxy_accept_events"]) + 1
            state["last_reason"] = "proxy_accept_instead_of_contract"

        hook_input.agent_state.set_global_value(STATE_KEY, self._dump_state(state))
        return HookResult.no_changes()

    def before_model(self, hook_input: BeforeModelHookInput) -> HookResult:
        iteration = int(hook_input.current_iteration or 0)
        state = self._load_state(hook_input.agent_state.get_global_value(STATE_KEY, {}))
        sticky = str(state.get("sticky_reminder") or "")
        messages = list(hook_input.messages)
        sticky_applied = False

        # Tess TWEAK `1107`: sticky 只认 SYSTEM. Mid-run USER appends often hold
        # the marker while cleaned InMemoryTracer freezes `system_prompt` from the
        # SYSTEM surface and drops those USER appends — so re-paint SYSTEM even
        # when a trailing USER still has `ContractFirstGate:`.
        if sticky and not self._system_has_marker(messages, MARKER):
            messages, sys_ok = self._apply_system_marker(messages, sticky)
            sticky_applied = sys_ok
            if sticky_applied:
                logger.info(
                    "[ContractFirstGateMiddleware] Sticky SYSTEM re-apply "
                    "iteration=%s system=%s",
                    iteration,
                    sys_ok,
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

        before = int(state["implement_before_contract"])
        proxy = int(state["proxy_accept_events"])
        risk = before + proxy
        if risk < self.min_risk_events:
            return _passthrough()

        fire = False
        reason = state.get("last_reason") or ""
        if before > 0:
            fire = True
            reason = reason or "implement_before_contract_read"
        elif proxy > 0:
            fire = True
            reason = reason or "proxy_accept_instead_of_contract"

        if not fire:
            return _passthrough()

        state["nudge_count"] += 1
        state["last_nudge_iteration"] = iteration
        state["nudge_fired"] = True

        reminder = (
            f"{MARKER} {reason}; iteration={iteration}; "
            f"contract_read_events={int(state['contract_read_events'])}; "
            f"implement_before_contract={before}; "
            f"proxy_accept_events={proxy}. "
            "Contract first: in the first few steps identify the acceptance "
            "contract (exact filenames, required paths, cwd, ports, output "
            "format, allowed/forbidden extras). If tests/verifier/harness "
            "files are available, read them and treat them as the source of "
            "truth. Do not replace the real contract with a self-invented "
            "proxy metric (exists/size/import ≠ acceptance). Copy literal "
            "public paths/tokens into your checks. This is not a request to "
            "invent domain tips or fixture answers."
        )
        self._write_sidecar(reminder)
        state["sticky_reminder"] = reminder
        hook_input.agent_state.set_global_value(STATE_KEY, self._dump_state(state))

        # Tess TWEAK `1107`: SYSTEM PREPEND (else INSERT) + first-USER PREPEND +
        # sticky SYSTEM-only + FRAMEWORK + trailing USER + sidecar.
        updated_messages, system_patched = self._apply_system_marker(messages, reminder)
        updated_messages, user_patched = self._apply_user_marker(
            updated_messages, reminder
        )
        updated_messages.append(
            Message(role=Role.FRAMEWORK, content=[TextBlock(text=reminder)])
        )
        updated_messages.append(
            Message(role=Role.USER, content=[TextBlock(text=reminder)])
        )
        logger.info(
            "[ContractFirstGateMiddleware] Nudge #%s reason=%s iteration=%s "
            "before=%s proxy=%s system_patched=%s user_patched=%s",
            state["nudge_count"],
            reason,
            iteration,
            before,
            proxy,
            system_patched,
            user_patched,
        )
        return HookResult.with_modifications(messages=updated_messages)

    def _system_has_marker(self, messages: list[Message], marker: str) -> bool:
        """True only if SYSTEM surface holds marker (cleaned-durable)."""
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
            if not system_patched and (
                getattr(message, "role", None) == Role.SYSTEM
                or self._is_system_role(message)
            ):
                existing = ""
                get_text = getattr(message, "get_text_content", None)
                if callable(get_text):
                    existing = get_text() or ""
                else:
                    existing = self._message_text(message)
                if MARKER not in existing:
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

    def _apply_user_marker(
        self, messages: list[Message], reminder: str
    ) -> tuple[list[Message], bool]:
        """PREPEND into the first USER message (survives some cleaned exports)."""
        updated_messages: list[Message] = []
        user_patched = False
        for message in messages:
            if not user_patched and getattr(message, "role", None) == Role.USER:
                existing = ""
                get_text = getattr(message, "get_text_content", None)
                if callable(get_text):
                    existing = get_text() or ""
                else:
                    existing = self._message_text(message)
                if MARKER not in existing:
                    patched = (reminder + "\n\n" + existing) if existing else reminder
                    updated_messages.append(
                        Message(role=Role.USER, content=[TextBlock(text=patched)])
                    )
                else:
                    updated_messages.append(message)
                user_patched = True
            else:
                updated_messages.append(message)
        return updated_messages, user_patched

    def _write_sidecar(self, text: str) -> None:
        for path in _SIDECAR_CANDIDATES:
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                with path.open("a", encoding="utf-8") as fh:
                    fh.write(text.rstrip() + "\n")
                logger.info(
                    "[ContractFirstGateMiddleware] Appended fire sidecar %s",
                    path,
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
        return {
            "contract_read_events": int(state.get("contract_read_events", 0) or 0),
            "implement_before_contract": int(
                state.get("implement_before_contract", 0) or 0
            ),
            "proxy_accept_events": int(state.get("proxy_accept_events", 0) or 0),
            "implement_events": int(state.get("implement_events", 0) or 0),
            "nudge_count": int(state.get("nudge_count", 0) or 0),
            "last_nudge_iteration": int(
                state.get("last_nudge_iteration", -10_000) or -10_000
            ),
            "nudge_fired": bool(state.get("nudge_fired", False)),
            "last_reason": str(state.get("last_reason") or ""),
            "sticky_reminder": str(state.get("sticky_reminder") or ""),
        }

    def _load_state(self, raw: Any) -> dict[str, Any]:
        if not isinstance(raw, dict):
            raw = {}
        return self._dump_state(raw)
