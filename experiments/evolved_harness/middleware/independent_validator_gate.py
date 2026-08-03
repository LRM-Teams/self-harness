"""Independent-validator gate — AHE §8 (Path C / LRM-1015).

Distinct from SHELVED SemanticNotProxy / LiteralContract / OfficialChecker /
RereadDeliverable / ContractFirst / CanonicalEntry, and from KEEP
EnumerateAllAnswers. Fires when a constrained DSL/config/script deliverable is
written or checked without evidence of an *independent* on-disk literal
allowlist / token-by-token check — especially when the same generator/regex
that produced the file is reused as the validator.

Inject (Tess TWEAK lesson from Enumerate `2000`): on risk in `after_tool`,
write sidecar + append tool note with literal `IndependentValidatorGate:`
(cleaned-durable); on next `before_model`, SYSTEM PREPEND/INSERT + first-USER
PREPEND + sticky SYSTEM-only + FRAMEWORK + trailing USER.
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

STATE_KEY = "independent_validator_gate_state"
MARKER = "IndependentValidatorGate:"
logger = logging.getLogger(__name__)

DELIVERABLE_WRITE_RE = re.compile(
    r"(?:"
    r"(?:>>?|tee\b)\s*/app/|"
    r"\b(?:cp|mv|install|rsync)\b[^\n]+/app/|"
    r"cat\s*>\s*/app/|"
    r"\bprintf\b[^\n]*>\s*/app/|"
    r"\bpython3?\s+[^\n]*\bopen\([^)]*['\"]/app/|"
    r"json\.dump\(|write_text\(|Path\([^)]*\)\.write|"
    r"\.(?:write|writelines|dump)\("
    r")",
    re.IGNORECASE,
)

# Same generator / regex reused as the validator (self-check).
SELF_VALIDATE_RE = re.compile(
    r"(?:"
    r"\b(?:same|the)\s+(?:regex|generator|pattern|script)\b|"
    r"\breuse\b[^\n]{0,40}(?:regex|generator|pattern)|"
    r"\b(?:validate|check|verify)\b[^\n]{0,80}\b(?:with|using)\b[^\n]{0,40}"
    r"(?:generator|regex|pattern)\b|"
    r"\b(?:re\.compile|re\.match|re\.fullmatch|re\.search)\b[^\n]{0,120}"
    r"(?:assert|==|validate|check)|"
    r"\bpython3?\s+-c\s+['\"][^'\"]{0,300}(?:write|open\([^)]*['\"]w)"
    r"[^'\"]{0,300}(?:assert|re\.|match\()"
    r")",
    re.IGNORECASE,
)

# Weak / existence-only checks that skip independent literal allowlist reads.
WEAK_CHECK_RE = re.compile(
    r"(?:"
    r"\bpytest\b|"
    r"\bpython3?\s+[^\n]*(?:test_|check_|verify_|validate_)[A-Za-z0-9_]+\.py\b|"
    r"\b(?:assert|diff|cmp)\b|"
    r"test\s+-[ef]\s+/app/|"
    r"\[\s+-f\s+/app/|"
    r"\bls\s+/app/"
    r")",
    re.IGNORECASE,
)

# Evidence of independent on-disk literal / token / line allowlist validation.
INDEPENDENT_CHECK_RE = re.compile(
    r"(?:"
    r"\ballowlist\b|\btoken[-_ ]by[-_ ]token\b|\bline[-_ ]by[-_ ]line\b|"
    r"\bindependent\s+(?:check|validat|verif)|"
    r"\bfrom\s+disk\b|\bon[-_ ]disk\b|"
    r"\b(?:cat|od|xxd|hexdump|nl|sed\s+-n)\s+/app/|"
    r"\bpython3?\s+-c\s+['\"][^'\"]{0,200}(?:open\([^)]*/app/|Path\([^)]*/app/)"
    r"[^'\"]{0,200}(?:read|readlines|splitlines)|"
    r"\bwhile\s+read\b|\bread\s+-r\b|"
    r"\bfor\s+\w+\s+in\b[^\n]{0,80}(?:readlines|splitlines|enumerate)"
    r")",
    re.IGNORECASE,
)

_SIDECAR_CANDIDATES = (
    Path("/logs/agent/independent_validator.nudge.txt"),
    Path("independent_validator.nudge.txt"),
)


class IndependentValidatorGateMiddleware(Middleware):
    """Nudge when DSL/script deliverables lack independent literal validation."""

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
            "write_events": 0,
            "self_validate_events": 0,
            "weak_check_events": 0,
            "independent_events": 0,
            "write_without_independent": 0,
            "self_validate_without_independent": 0,
            "weak_check_without_independent": 0,
            "nudge_count": 0,
            "last_nudge_iteration": -10_000,
            "nudge_fired": False,
            "last_reason": "",
            "sticky_reminder": "",
            "message_inject_pending": False,
        }
        hook_input.agent_state.set_global_value(STATE_KEY, self._dump_state(state))
        return HookResult.no_changes()

    def after_tool(self, hook_input: AfterToolHookInput) -> HookResult:
        if hook_input.tool_name != "run_shell_command":
            return HookResult.no_changes()

        command = self._extract_command(hook_input.tool_input)
        state = self._load_state(hook_input.agent_state.get_global_value(STATE_KEY, {}))

        if INDEPENDENT_CHECK_RE.search(command):
            state["independent_events"] = int(state["independent_events"]) + 1
            state["last_reason"] = "independent_check_ok"

        if DELIVERABLE_WRITE_RE.search(command):
            state["write_events"] = int(state["write_events"]) + 1
            if int(state["independent_events"]) <= 0:
                state["write_without_independent"] = (
                    int(state["write_without_independent"]) + 1
                )
                state["last_reason"] = "deliverable_write_without_independent_validator"
            else:
                state["last_reason"] = "deliverable_write_after_independent"

        if SELF_VALIDATE_RE.search(command):
            state["self_validate_events"] = int(state["self_validate_events"]) + 1
            if int(state["independent_events"]) <= 0:
                state["self_validate_without_independent"] = (
                    int(state["self_validate_without_independent"]) + 1
                )
                state["last_reason"] = "generator_or_regex_reused_as_validator"
            else:
                state["last_reason"] = "self_validate_with_prior_independent"

        if WEAK_CHECK_RE.search(command) and not INDEPENDENT_CHECK_RE.search(command):
            state["weak_check_events"] = int(state["weak_check_events"]) + 1
            if int(state["independent_events"]) <= 0:
                state["weak_check_without_independent"] = (
                    int(state["weak_check_without_independent"]) + 1
                )
                if not str(state.get("last_reason") or "").startswith("generator"):
                    state["last_reason"] = "weak_check_without_independent_validator"

        reminder = ""
        tool_output = hook_input.tool_output
        write_wo = int(state["write_without_independent"])
        self_wo = int(state["self_validate_without_independent"])
        weak_wo = int(state["weak_check_without_independent"])
        risk = write_wo + self_wo + weak_wo
        if (
            int(state["nudge_count"]) == 0
            and int(state["independent_events"]) <= 0
            and risk >= self.min_risk_events
            and (write_wo > 0 or self_wo > 0 or weak_wo > 0)
        ):
            reason = str(
                state.get("last_reason")
                or "deliverable_write_without_independent_validator"
            )
            reminder = self._build_reminder(
                reason=reason,
                iteration=-1,
                write_events=int(state["write_events"]),
                self_validate_events=int(state["self_validate_events"]),
                weak_check_events=int(state["weak_check_events"]),
                independent_events=int(state["independent_events"]),
                write_wo=write_wo,
                self_wo=self_wo,
                weak_wo=weak_wo,
            )
            self._write_sidecar(reminder)
            state["nudge_count"] = 1
            state["last_nudge_iteration"] = 0
            state["nudge_fired"] = True
            state["sticky_reminder"] = reminder
            state["message_inject_pending"] = True
            tool_output = self._append_tool_note(tool_output, reminder)
            logger.info(
                "[IndependentValidatorGateMiddleware] after_tool fire #1 reason=%s "
                "write_wo=%s self_wo=%s weak_wo=%s indep=%s "
                "(sidecar+tool_note; pending message inject)",
                reason,
                write_wo,
                self_wo,
                weak_wo,
                int(state["independent_events"]),
            )

        hook_input.agent_state.set_global_value(STATE_KEY, self._dump_state(state))
        if reminder:
            return HookResult.with_modifications(tool_output=tool_output)
        return HookResult.no_changes()

    def before_model(self, hook_input: BeforeModelHookInput) -> HookResult:
        iteration = int(hook_input.current_iteration or 0)
        state = self._load_state(hook_input.agent_state.get_global_value(STATE_KEY, {}))
        sticky = str(state.get("sticky_reminder") or "")
        messages = list(hook_input.messages)
        sticky_applied = False
        pending_inject = bool(state.get("message_inject_pending"))

        if sticky and (
            pending_inject
            or not self._system_has_marker(messages, MARKER)
            or not self._user_has_marker(messages, MARKER)
        ):
            messages, sys_ok = self._apply_system_marker(messages, sticky)
            messages, user_ok = self._apply_user_marker(messages, sticky)
            sticky_applied = sys_ok or user_ok
            if sticky_applied:
                logger.info(
                    "[IndependentValidatorGateMiddleware] Sticky SYSTEM/USER re-apply "
                    "iteration=%s system=%s user=%s pending=%s",
                    iteration,
                    sys_ok,
                    user_ok,
                    pending_inject,
                )

        def _passthrough() -> HookResult:
            if pending_inject and sticky:
                updated = list(messages)
                updated.append(
                    Message(role=Role.FRAMEWORK, content=[TextBlock(text=sticky)])
                )
                updated.append(
                    Message(role=Role.USER, content=[TextBlock(text=sticky)])
                )
                state["message_inject_pending"] = False
                state["last_nudge_iteration"] = iteration
                hook_input.agent_state.set_global_value(STATE_KEY, self._dump_state(state))
                return HookResult.with_modifications(messages=updated)
            if sticky_applied:
                hook_input.agent_state.set_global_value(STATE_KEY, self._dump_state(state))
                return HookResult.with_modifications(messages=messages)
            return HookResult.no_changes()

        if iteration < self.min_iterations_before_nudge:
            return _passthrough()

        has_assistant = any(
            getattr(message, "role", None) == Role.ASSISTANT for message in messages
        )
        if not has_assistant:
            return _passthrough()

        if pending_inject:
            return _passthrough()

        if state["nudge_count"] >= self.max_nudges:
            return _passthrough()
        if (
            state["nudge_count"] > 0
            and iteration - state["last_nudge_iteration"] < self.renudge_every_iterations
        ):
            return _passthrough()

        write_wo = int(state["write_without_independent"])
        self_wo = int(state["self_validate_without_independent"])
        weak_wo = int(state["weak_check_without_independent"])
        risk = write_wo + self_wo + weak_wo
        if risk < self.min_risk_events:
            return _passthrough()

        fire = False
        reason = state.get("last_reason") or ""
        if self_wo > 0 and int(state["independent_events"]) <= 0:
            fire = True
            reason = reason or "generator_or_regex_reused_as_validator"
        elif write_wo > 0 and int(state["independent_events"]) <= 0:
            fire = True
            reason = reason or "deliverable_write_without_independent_validator"
        elif weak_wo > 0 and int(state["independent_events"]) <= 0:
            fire = True
            reason = reason or "weak_check_without_independent_validator"

        if not fire:
            return _passthrough()

        state["nudge_count"] = int(state["nudge_count"]) + 1
        state["last_nudge_iteration"] = iteration
        state["nudge_fired"] = True
        reminder = self._build_reminder(
            reason=reason,
            iteration=iteration,
            write_events=int(state["write_events"]),
            self_validate_events=int(state["self_validate_events"]),
            weak_check_events=int(state["weak_check_events"]),
            independent_events=int(state["independent_events"]),
            write_wo=write_wo,
            self_wo=self_wo,
            weak_wo=weak_wo,
        )
        self._write_sidecar(reminder)
        state["sticky_reminder"] = reminder
        state["message_inject_pending"] = False
        hook_input.agent_state.set_global_value(STATE_KEY, self._dump_state(state))

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
            "[IndependentValidatorGateMiddleware] Nudge #%s reason=%s iteration=%s "
            "write_wo=%s self_wo=%s weak_wo=%s indep=%s system_patched=%s user_patched=%s",
            state["nudge_count"],
            reason,
            iteration,
            write_wo,
            self_wo,
            weak_wo,
            int(state["independent_events"]),
            system_patched,
            user_patched,
        )
        return HookResult.with_modifications(messages=updated_messages)

    def _build_reminder(
        self,
        *,
        reason: str,
        iteration: int,
        write_events: int,
        self_validate_events: int,
        weak_check_events: int,
        independent_events: int,
        write_wo: int,
        self_wo: int,
        weak_wo: int,
    ) -> str:
        return (
            f"{MARKER} {reason}; iteration={iteration}; "
            f"write_events={write_events}; "
            f"self_validate_events={self_validate_events}; "
            f"weak_check_events={weak_check_events}; "
            f"independent_events={independent_events}; "
            f"write_without_independent={write_wo}; "
            f"self_validate_without_independent={self_wo}; "
            f"weak_check_without_independent={weak_wo}. "
            "Use semantic checks, then stop: for constrained DSL/config/script "
            "outputs, validate the final file from disk line-by-line or "
            "token-by-token against the contract's literal allowlist; do not "
            "reuse the same regex or generator that produced the file as the "
            "validator. This is not a request to invent domain tips or fixture "
            "answers."
        )

    def _extract_command(self, tool_input: Any) -> str:
        if not isinstance(tool_input, dict):
            return ""
        direct = tool_input.get("command")
        if isinstance(direct, str) and direct.strip():
            return direct
        params = tool_input.get("parameters")
        if isinstance(params, dict):
            nested = params.get("command")
            if isinstance(nested, str) and nested.strip():
                return nested
        return ""

    def _append_tool_note(self, tool_output: Any, reminder: str) -> Any:
        note = reminder if reminder.startswith(MARKER) else f"{MARKER} {reminder}"
        line = f"Harness note: {note}"
        if isinstance(tool_output, dict):
            updated = dict(tool_output)
            existing = ""
            content = updated.get("content")
            if isinstance(content, str):
                existing = content
            elif content is not None:
                existing = str(content)
            if MARKER in existing:
                return updated
            updated["content"] = existing + ("\n" if existing else "") + line
            return updated
        existing = tool_output if isinstance(tool_output, str) else str(tool_output or "")
        if MARKER in existing:
            return tool_output
        return existing + ("\n" if existing else "") + line

    def _system_has_marker(self, messages: list[Message], marker: str) -> bool:
        for message in messages:
            if self._is_system_role(message) and marker in self._message_text(message):
                return True
        return False

    def _user_has_marker(self, messages: list[Message], marker: str) -> bool:
        for message in messages:
            if getattr(message, "role", None) == Role.USER and marker in self._message_text(
                message
            ):
                return True
        return False

    def _apply_system_marker(
        self, messages: list[Message], reminder: str
    ) -> tuple[list[Message], bool]:
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
                    "[IndependentValidatorGateMiddleware] Appended fire sidecar %s",
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
            "write_events": int(state.get("write_events", 0) or 0),
            "self_validate_events": int(state.get("self_validate_events", 0) or 0),
            "weak_check_events": int(state.get("weak_check_events", 0) or 0),
            "independent_events": int(state.get("independent_events", 0) or 0),
            "write_without_independent": int(
                state.get("write_without_independent", 0) or 0
            ),
            "self_validate_without_independent": int(
                state.get("self_validate_without_independent", 0) or 0
            ),
            "weak_check_without_independent": int(
                state.get("weak_check_without_independent", 0) or 0
            ),
            "nudge_count": int(state.get("nudge_count", 0) or 0),
            "last_nudge_iteration": int(
                state.get("last_nudge_iteration", -10_000) or -10_000
            ),
            "nudge_fired": bool(state.get("nudge_fired", False)),
            "last_reason": str(state.get("last_reason") or ""),
            "sticky_reminder": str(state.get("sticky_reminder") or ""),
            "message_inject_pending": bool(state.get("message_inject_pending", False)),
        }

    def _load_state(self, raw: Any) -> dict[str, Any]:
        if not isinstance(raw, dict):
            raw = {}
        return self._dump_state(raw)
