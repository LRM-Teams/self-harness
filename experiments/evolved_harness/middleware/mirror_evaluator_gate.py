"""Mirror-evaluator gate — AHE §2 (Path C / LRM-1009).

Distinct from SHELVED RereadDeliverable (disk reread only), OfficialChecker
(mid-loop official checker), EndStateReady (env ready sweep), FailFast,
StopAfterPass, SemanticNotProxy, ContractFirst, LiteralContract, CanonicalEntry.

Fires when the agent writes/implements deliverables or relies on proxy
signals without an independent evaluator-mirroring final check (same cwd /
exact public path / pytest-or-documented runner / services left running) —
process pressure only, never domain tips or fixture answers.
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

STATE_KEY = "mirror_evaluator_gate_state"
MARKER = "MirrorEvaluatorGate:"
logger = logging.getLogger(__name__)

# Wrote / published deliverables under /app (or common outputs).
IMPLEMENT_WRITE_RE = re.compile(
    r"(?:"
    r"(?:>>?|tee\b)\s*/app/|"
    r"\b(?:cp|mv|install|rsync)\b[^\n]+/app/|"
    r"cat\s*>\s*/app/|"
    r"\bpython3?\s+[^\n]*\bopen\([^)]*['\"]/app/|"
    r"\bgcc\b|\bg\+\+|\bclang\b|\brustc\b|\bmake\b|\bcargo\s+build|"
    r"\bprintf\b[^\n]*>\s*/app/"
    r")",
    re.IGNORECASE,
)

# Independent evaluator-mirroring checks (pytest / documented runner /
# exact public entry / semantic recompute from on-disk artifact).
MIRROR_CHECK_RE = re.compile(
    r"(?:"
    r"\bpytest\b|"
    r"\bpython3?\s+-m\s+pytest\b|"
    r"\b(?:bash|sh)\s+[^\n]*run-tests\.sh\b|"
    r"\bpython3?\s+[^\n]*test_outputs\.py\b|"
    r"\bcurl\b[^\n]*(?:localhost|127\.0\.0\.1)|"
    r"\bwget\b[^\n]*(?:localhost|127\.0\.0\.1)|"
    r"\bnc\s+-z\b|"
    r"\bcmp\b|\bdiff\b|\bsha256sum\b|\bmd5sum\b"
    r")",
    re.IGNORECASE,
)

# Proxy "acceptance" that is not an evaluator mirror.
PROXY_ACCEPT_RE = re.compile(
    r"(?:"
    r"\btest\s+-[ef]\s+/app/|"
    r"\b\[+\s+-[ef]\s+/app/|"
    r"\bls\s+-l\s+/app/[^\n]*&&|"
    r"\bwc\s+-c\s+/app/|"
    r"\bstat\s+/app/|"
    r"\bpython3?\s+-c\s+['\"][^'\"]*\bimport\b[^'\"]*['\"]"
    r")",
    re.IGNORECASE,
)

# Started a long-lived service/server.
SERVICE_START_RE = re.compile(
    r"(?:"
    r"\bis_background\b|"  # rarely appears in command; keep shell patterns
    r"\bnohup\b|\bsystemctl\s+start\b|\bservice\s+\w+\s+start\b|"
    r"\bpython3?\s+[^\n]*\b(?:http\.server|uvicorn|gunicorn|flask)\b|"
    r"\bnginx\b|\bnode\s+[^\n]+\.js\b"
    r")",
    re.IGNORECASE,
)

# Stopped / killed services without a follow-up mirror check.
SERVICE_STOP_RE = re.compile(
    r"(?:"
    r"\b(?:pkill|killall|kill)\b|"
    r"\bsystemctl\s+stop\b|\bservice\s+\w+\s+stop\b|"
    r"\bdocker\s+stop\b"
    r")",
    re.IGNORECASE,
)

_SIDECAR_CANDIDATES = (
    Path("/logs/agent/mirror_evaluator.nudge.txt"),
    Path("mirror_evaluator.nudge.txt"),
)


class MirrorEvaluatorGateMiddleware(Middleware):
    """Nudge when work exists without an independent evaluator-mirroring check."""

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
            "implement_events": 0,
            "mirror_check_events": 0,
            "proxy_accept_events": 0,
            "service_start_events": 0,
            "service_stop_without_mirror": 0,
            "implement_without_mirror": 0,
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

        if MIRROR_CHECK_RE.search(command):
            state["mirror_check_events"] = int(state["mirror_check_events"]) + 1
            state["last_reason"] = "mirror_check_ok"

        if IMPLEMENT_WRITE_RE.search(command):
            state["implement_events"] = int(state["implement_events"]) + 1
            if int(state["mirror_check_events"]) <= 0:
                state["implement_without_mirror"] = (
                    int(state["implement_without_mirror"]) + 1
                )
                state["last_reason"] = "implement_without_mirror_check"
            else:
                state["last_reason"] = "implement_with_prior_mirror"

        if PROXY_ACCEPT_RE.search(command) and int(state["mirror_check_events"]) <= 0:
            state["proxy_accept_events"] = int(state["proxy_accept_events"]) + 1
            state["last_reason"] = "proxy_accept_instead_of_mirror"

        if SERVICE_START_RE.search(command):
            state["service_start_events"] = int(state["service_start_events"]) + 1
            state["last_reason"] = "service_started"

        if SERVICE_STOP_RE.search(command):
            if (
                int(state["service_start_events"]) > 0
                and int(state["mirror_check_events"]) <= 0
            ):
                state["service_stop_without_mirror"] = (
                    int(state["service_stop_without_mirror"]) + 1
                )
                state["last_reason"] = "service_stop_without_mirror_recheck"

        hook_input.agent_state.set_global_value(STATE_KEY, self._dump_state(state))
        return HookResult.no_changes()

    def before_model(self, hook_input: BeforeModelHookInput) -> HookResult:
        iteration = int(hook_input.current_iteration or 0)
        state = self._load_state(hook_input.agent_state.get_global_value(STATE_KEY, {}))
        sticky = str(state.get("sticky_reminder") or "")
        messages = list(hook_input.messages)
        sticky_applied = False

        # Tess TWEAK `1623`: sticky 只认 SYSTEM. Cleaned InMemoryTracer freezes
        # system_prompt from SYSTEM; re-paint even if USER/FRAMEWORK still hold
        # the marker (those roles often get stripped from cleaned export).
        if sticky and not self._system_has_marker(messages, MARKER):
            messages, sys_ok = self._apply_system_marker(messages, sticky)
            sticky_applied = sys_ok
            if sticky_applied:
                logger.info(
                    "[MirrorEvaluatorGateMiddleware] Sticky SYSTEM re-apply "
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

        without = int(state["implement_without_mirror"])
        proxy = int(state["proxy_accept_events"])
        svc_stop = int(state["service_stop_without_mirror"])
        risk = without + proxy + svc_stop
        if risk < self.min_risk_events:
            return _passthrough()

        fire = False
        reason = state.get("last_reason") or ""
        if without > 0 and int(state["mirror_check_events"]) <= 0:
            fire = True
            reason = reason or "implement_without_mirror_check"
        elif proxy > 0 and int(state["mirror_check_events"]) <= 0:
            fire = True
            reason = reason or "proxy_accept_instead_of_mirror"
        elif svc_stop > 0 and int(state["mirror_check_events"]) <= 0:
            fire = True
            reason = reason or "service_stop_without_mirror_recheck"

        if not fire:
            return _passthrough()

        state["nudge_count"] += 1
        state["last_nudge_iteration"] = iteration
        state["nudge_fired"] = True

        reminder = (
            f"{MARKER} {reason}; iteration={iteration}; "
            f"implement_events={int(state['implement_events'])}; "
            f"mirror_check_events={int(state['mirror_check_events'])}; "
            f"proxy_accept_events={proxy}; "
            f"service_stop_without_mirror={svc_stop}. "
            "Mirror the evaluator before finishing: separate implementation "
            "from acceptance. Run an independent final check that mirrors the "
            "evaluator (same cwd, exact public path/socket/filename/port, "
            "pytest or the task's documented runner). For relative outputs, "
            "confirm from a scratch cwd outside the source tree. Leave "
            "services running and re-verify reachability. Check required "
            "files and forbidden extras. If a check disagrees with your "
            "theory, trust the check. This is not a request to invent domain "
            "tips or fixture answers."
        )
        self._write_sidecar(reminder)
        state["sticky_reminder"] = reminder
        hook_input.agent_state.set_global_value(STATE_KEY, self._dump_state(state))

        # SYSTEM PREPEND (else INSERT) + first-USER PREPEND + sticky SYSTEM +
        # FRAMEWORK + trailing USER + sidecar; literal MirrorEvaluatorGate:.
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
            "[MirrorEvaluatorGateMiddleware] Nudge #%s reason=%s iteration=%s "
            "without=%s proxy=%s svc_stop=%s system_patched=%s user_patched=%s",
            state["nudge_count"],
            reason,
            iteration,
            without,
            proxy,
            svc_stop,
            system_patched,
            user_patched,
        )
        return HookResult.with_modifications(messages=updated_messages)

    def _system_has_marker(self, messages: list[Message], marker: str) -> bool:
        for message in messages:
            if self._is_system_role(message) and marker in self._message_text(message):
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
                    "[MirrorEvaluatorGateMiddleware] Appended fire sidecar %s",
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
            "implement_events": int(state.get("implement_events", 0) or 0),
            "mirror_check_events": int(state.get("mirror_check_events", 0) or 0),
            "proxy_accept_events": int(state.get("proxy_accept_events", 0) or 0),
            "service_start_events": int(state.get("service_start_events", 0) or 0),
            "service_stop_without_mirror": int(
                state.get("service_stop_without_mirror", 0) or 0
            ),
            "implement_without_mirror": int(
                state.get("implement_without_mirror", 0) or 0
            ),
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
