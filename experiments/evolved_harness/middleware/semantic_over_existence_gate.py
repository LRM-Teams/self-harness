"""Semantic-over-existence gate — AHE §8 (Path C / LRM-1028).

Distinct from SHELVED RecomputeFromArtifact / EndStateReady / SemanticNotProxy /
PreserveSemantics / OfficialChecker / FailFast / TrustCheck, and from KEEP
IndependentValidator / BoundaryCase / ForbiddenExtras / Enumerate / SanitySmoke /
APG.

Fires when a deliverable is written or weakly checked for mere existence /
import / size / build / row-count success without evidence of an evaluator-facing
*semantic* check on actual values or behavior. Does not feed fixture answers
(frame bounds, WAL value 150, etc.).

Inject (Enumerate `2000` + Tess TWEAK `2355`): on risk in `after_tool`,
write sidecar + append tool note with literal `SemanticOverExistenceGate:`
(cleaned-durable); on next `before_model`, SYSTEM PREPEND/INSERT + first-USER
PREPEND + sticky SYSTEM-prefer + FRAMEWORK + trailing USER. Semantic clear
requires assert/value check — not bare cv2/ffprobe/tomllib/field names.
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

STATE_KEY = "semantic_over_existence_gate_state"
MARKER = "SemanticOverExistenceGate:"
logger = logging.getLogger(__name__)

WRITE_RE = re.compile(
    r"(?:"
    r"(?:>>?|tee\b)\s*/app/|"
    r"\b(?:cp|mv|install|rsync)\b[^\n]+/app/|"
    r"cat\s*>\s*/app/|"
    r"\bprintf\b[^\n]*>\s*/app/|"
    r"\bpython3?\s+[^\n]*\bopen\([^)]*['\"]/app/|"
    r"json\.dump\(|write_text\(|Path\([^)]*\)\.write|"
    r"\.(?:write|writelines|dump|to_csv|savefig|to_sql)\("
    r")",
    re.IGNORECASE,
)

# Existence / import / size / build / row-count / field-presence proxies.
# (Tess TWEAK `2355`: video wrote output.toml with wrong frames but never
# tripped existence/write risk — broaden proxies; do not feed fixture answers.)
EXISTENCE_WEAK_RE = re.compile(
    r"(?:"
    r"test\s+-[ef]\s+/app/|"
    r"\[\s+-[ef]\s+/app/|"
    r"\bls\s+/app/|"
    r"\b(?:stat|wc\s+-c|du\b)\s+/app/|"
    r"\bcat\s+/app/(?:output\.toml|recovered\.json|main\.db)\b|"
    r"\b(?:importlib|__import__|\bimport\s+\w+)\b|"
    r"\bPath\([^)]*\)\.exists\(|"
    r"\bos\.path\.exists\(|"
    r"\b(?:file|path)\s+(?:exists|present|created)\b|"
    r"\b(?:compile|build)\s+(?:ok|success|succeeded)\b|"
    r"\blen\s*\([^)]*\)\s*(?:==|>|>=)\s*\d|"
    r"\b(?:row|record|entry)\s*count\b|"
    r"\bSELECT\s+COUNT\s*\(|"
    r"output\.toml\s+(?:exists|created|present)|"
    r"\b(?:keys?|fields?)\s+(?:present|exist|ok|complete)\b|"
    r"\bassert\s+\w+\.exists\("
    r")",
    re.IGNORECASE,
)

# Evidence of an evaluator-facing *value/behavior* check on the submitted
# artifact. Mere library use (cv2/ffprobe/tomllib) or field-name mentions in
# solution code must NOT clear risk — that false-positive silenced video on
# `2355` (fire N despite 有产物仍语义错).
SEMANTIC_CHECK_RE = re.compile(
    r"(?:"
    r"\bsemantic\s+(?:check|validat|verif|assert)\b|"
    r"\bevaluator[-_ ]facing\b|"
    r"\bassert\b[^\n]{0,200}(?:==|!=|<|>|<=|>=|\bin\b|not\s+in\b)|"
    r"\b(?:==|!=)\s*\d+[^\n]{0,80}"
    r"(?:value|frame|wal|apple|landing|takeoff|decrypt|payload)|"
    r"(?:value|frame|wal|apple|landing|takeoff|decrypt|payload)"
    r"[^\n]{0,80}\b(?:==|!=)\s*\d+|"
    r"\b(?:tomllib|toml)\.load\b[^\n]{0,160}\bassert\b|"
    r"\bjson\.load\b[^\n]{0,120}\bassert\b|"
    r"\bsqlite3\b[^\n]{0,160}"
    r"(?:\bassert\b|==\s*\d+|SELECT[^\n]{0,60}WHERE)|"
    r"\b(?:takeoff|landing)_frame[^\n]{0,80}"
    r"(?:\bassert\b|==|!=|in\s*\[)|"
    r"\bWAL\b[^\n]{0,100}"
    r"(?:appl(?:y|ied)|decrypt|recover)[^\n]{0,60}"
    r"(?:\bassert\b|==|!=)|"
    r"\b(?:diff|cmp)\b\s+/app/"
    r")",
    re.IGNORECASE,
)

_SIDECAR_CANDIDATES = (
    Path("/logs/agent/semantic_over_existence.nudge.txt"),
    Path("semantic_over_existence.nudge.txt"),
)


class SemanticOverExistenceGateMiddleware(Middleware):
    """Nudge when existence/import/size proxies replace semantic checks."""

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
            "existence_events": 0,
            "semantic_events": 0,
            "write_without_semantic": 0,
            "existence_without_semantic": 0,
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
        output_text = self._tool_output_text(hook_input.tool_output)
        blob = f"{command}\n{output_text}"
        state = self._load_state(hook_input.agent_state.get_global_value(STATE_KEY, {}))

        if SEMANTIC_CHECK_RE.search(blob):
            state["semantic_events"] = int(state["semantic_events"]) + 1
            state["last_reason"] = "semantic_check_ok"

        if EXISTENCE_WEAK_RE.search(blob):
            state["existence_events"] = int(state["existence_events"]) + 1
            if int(state["semantic_events"]) <= 0:
                state["existence_without_semantic"] = (
                    int(state["existence_without_semantic"]) + 1
                )
                state["last_reason"] = "existence_proxy_without_semantic_check"

        if WRITE_RE.search(command) or WRITE_RE.search(blob):
            state["write_events"] = int(state["write_events"]) + 1
            if int(state["semantic_events"]) <= 0:
                state["write_without_semantic"] = (
                    int(state["write_without_semantic"]) + 1
                )
                state["last_reason"] = "write_without_semantic_check"

        reminder = ""
        tool_output = hook_input.tool_output
        write_wo = int(state["write_without_semantic"])
        exist_wo = int(state["existence_without_semantic"])
        semantic = int(state["semantic_events"])
        risk = write_wo + exist_wo
        if (
            int(state["nudge_count"]) == 0
            and semantic <= 0
            and risk >= self.min_risk_events
            and (write_wo > 0 or exist_wo > 0)
        ):
            reason = str(
                state.get("last_reason") or "existence_proxy_without_semantic_check"
            )
            reminder = self._build_reminder(
                reason=reason,
                iteration=-1,
                write_events=int(state["write_events"]),
                existence_events=int(state["existence_events"]),
                semantic_events=semantic,
                write_without_semantic=write_wo,
                existence_without_semantic=exist_wo,
            )
            self._write_sidecar(reminder)
            state["nudge_count"] = 1
            state["last_nudge_iteration"] = 0
            state["nudge_fired"] = True
            state["sticky_reminder"] = reminder
            state["message_inject_pending"] = True
            tool_output = self._append_tool_note(tool_output, reminder)
            logger.info(
                "[SemanticOverExistenceGateMiddleware] after_tool fire #1 reason=%s "
                "write_wo=%s exist_wo=%s semantic=%s "
                "(sidecar+tool_note; pending message inject)",
                reason,
                write_wo,
                exist_wo,
                semantic,
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

        # Tess TWEAK: cleaned freezes first system_prompt — sticky prefers
        # SYSTEM; still PREPEND first-USER when pending/first inject.
        if sticky and (
            pending_inject or not self._system_has_marker(messages, MARKER)
        ):
            messages, sys_ok = self._apply_system_marker(messages, sticky)
            user_ok = False
            if pending_inject or not self._user_has_marker(messages, MARKER):
                messages, user_ok = self._apply_user_marker(messages, sticky)
            sticky_applied = sys_ok or user_ok
            if sticky_applied:
                logger.info(
                    "[SemanticOverExistenceGateMiddleware] Sticky SYSTEM/USER "
                    "re-apply iteration=%s system=%s user=%s pending=%s",
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

        write_wo = int(state["write_without_semantic"])
        exist_wo = int(state["existence_without_semantic"])
        semantic = int(state["semantic_events"])
        if semantic > 0:
            return _passthrough()
        risk = write_wo + exist_wo
        if risk < self.min_risk_events:
            return _passthrough()

        fire = False
        reason = state.get("last_reason") or ""
        if write_wo > 0:
            fire = True
            reason = reason or "write_without_semantic_check"
        elif exist_wo > 0:
            fire = True
            reason = reason or "existence_proxy_without_semantic_check"

        if not fire:
            return _passthrough()

        state["nudge_count"] = int(state["nudge_count"]) + 1
        state["last_nudge_iteration"] = iteration
        state["nudge_fired"] = True
        reminder = self._build_reminder(
            reason=reason,
            iteration=iteration,
            write_events=int(state["write_events"]),
            existence_events=int(state["existence_events"]),
            semantic_events=semantic,
            write_without_semantic=write_wo,
            existence_without_semantic=exist_wo,
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
            "[SemanticOverExistenceGateMiddleware] Nudge #%s reason=%s iteration=%s "
            "write_wo=%s exist_wo=%s semantic=%s "
            "system_patched=%s user_patched=%s",
            state["nudge_count"],
            reason,
            iteration,
            write_wo,
            exist_wo,
            semantic,
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
        existence_events: int,
        semantic_events: int,
        write_without_semantic: int,
        existence_without_semantic: int,
    ) -> str:
        return (
            f"{MARKER} {reason}; iteration={iteration}; "
            f"write_events={write_events}; "
            f"existence_events={existence_events}; "
            f"semantic_events={semantic_events}; "
            f"write_without_semantic={write_without_semantic}; "
            f"existence_without_semantic={existence_without_semantic}. "
            "Do not treat import success, file existence, file size, build "
            "success, or field/row presence as sufficient by themselves; pair "
            "them with at least one evaluator-facing semantic check on the "
            "actual output/behavior. After writing the deliverable, re-read it "
            "and assert the contract's real values/behavior (numeric fields, "
            "applied updates), not just that a path or schema looks present. "
            "This is not a request to invent domain tips or fixture answers."
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

    def _tool_output_text(self, tool_output: Any) -> str:
        if isinstance(tool_output, dict):
            content = tool_output.get("content")
            if isinstance(content, str):
                return content
            if content is not None:
                return str(content)
            return str(tool_output)
        if isinstance(tool_output, str):
            return tool_output
        return str(tool_output or "")

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
                    "[SemanticOverExistenceGateMiddleware] Appended fire sidecar %s",
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
            "existence_events": int(state.get("existence_events", 0) or 0),
            "semantic_events": int(state.get("semantic_events", 0) or 0),
            "write_without_semantic": int(state.get("write_without_semantic", 0) or 0),
            "existence_without_semantic": int(
                state.get("existence_without_semantic", 0) or 0
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
