"""Short-probe-first gate — AHE §6 (Path C / LRM-996).

Distinct from SHELVED TimeBudget (near-deadline force publish/stop),
PersistBestCandidate (persist best-so-far before another long run),
CheckpointDeadline, PublishPressure, and ExecutionRiskHints.

Fires when the agent burns budget on long foreground probes without
``timeout_ms``, re-probes an unavailable environment, or starts expensive
train/search work before any short probe — process pressure only, never
domain tips or fixture answers.
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

STATE_KEY = "short_probe_first_gate_state"
MARKER = "ShortProbeFirstGate:"
logger = logging.getLogger(__name__)

# Expensive / long-horizon work that should follow a short probe (and prefer bg).
LONG_WORK_RE = re.compile(
    r"(?:"
    r"\b(?:train(?:ing)?|optimize|solver|epochs?|fit\(|caffe|mujoco|"
    r"fasttext\.train|fasttext\s+supervised|hyperopt|grid.?search|"
    r"bayes|fine.?tun|autotune|sweep|random.?search|optuna|"
    r"make\s+-j|cmake\s+--build|pip\s+install\b[^\n]{20,}|"
    r"apt-?get\s+install|npm\s+install|yarn\s+install|"
    r"wget\b|curl\s+-O|torchrun|python3?\s+[^\n]*(?:train|fit|solve)"
    r")\b"
    r")",
    re.IGNORECASE,
)
# Short probe / inspection signals (process-level).
SHORT_PROBE_RE = re.compile(
    r"(?:"
    r"\bls\b|\bhead\b|\btail\b|\bwc\b|\bfile\b|\bstat\b|"
    r"\bpython3?\s+-c\s+['\"][^'\"]{0,160}['\"]|"
    r"\bwhich\b|\bcommand\s+-v\b|\btype\b|"
    r"\btest\s+-[ef]\b|\bdu\s+-|"
    r"\btimeout\s+\d+\b|"
    r"\bcat\s+[^\n|]{1,80}$"
    r")",
    re.IGNORECASE,
)
# Env availability re-probe (stop once confirmed missing/present).
ENV_PROBE_RE = re.compile(
    r"(?:"
    r"\bnvidia-smi\b|\bnvcc\b|\bhipcc\b|"
    r"\bpip\s+show\b|\bpip\s+list\b|"
    r"\bconda\s+list\b|\bdpkg\s+-l\b|"
    r"\blsmod\b|\blspci\b|"
    r"\bpython3?\s+-c\s+['\"][^'\"]*(?:import\s+(?:torch|tensorflow|caffe)|"
    r"torch\.cuda)[^'\"]*['\"]"
    r")",
    re.IGNORECASE,
)
SLEEP_POLL_RE = re.compile(
    r"(?:"
    r"\bsleep\s+\d{2,}|"
    r"\bwhile\s+true\b|"
    r"\bfor\s+\w+\s+in\s+\{1\.\.\d{2,}"
    r")",
    re.IGNORECASE,
)
TIMEOUT_MS_RE = re.compile(r"\btimeout_ms\b", re.IGNORECASE)

_SIDECAR_CANDIDATES = (
    Path("/logs/agent/short_probe_first.nudge.txt"),
    Path("short_probe_first.nudge.txt"),
)


class ShortProbeFirstGateMiddleware(Middleware):
    """Nudge when long work / long foreground probes skip short-probe hygiene."""

    def __init__(
        self,
        *,
        min_iterations_before_nudge: int = 2,
        renudge_every_iterations: int = 8,
        max_nudges: int = 3,
        long_step_ms: int = 90_000,
        min_env_probe_repeats: int = 2,
    ) -> None:
        self.min_iterations_before_nudge = int(min_iterations_before_nudge)
        self.renudge_every_iterations = int(renudge_every_iterations)
        self.max_nudges = int(max_nudges)
        self.long_step_ms = int(long_step_ms)
        self.min_env_probe_repeats = int(min_env_probe_repeats)

    def before_agent(self, hook_input: BeforeAgentHookInput) -> HookResult:
        state = {
            "short_probe_events": 0,
            "long_work_events": 0,
            "long_fg_no_timeout": 0,
            "env_probe_events": 0,
            "sleep_poll_events": 0,
            "bg_long_events": 0,
            "nudge_count": 0,
            "last_nudge_iteration": -10_000,
            "nudge_fired": False,
            "last_reason": "",
            "sticky_reminder": "",
            "seen_env_probes": [],
        }
        hook_input.agent_state.set_global_value(STATE_KEY, self._dump_state(state))
        return HookResult.no_changes()

    def after_tool(self, hook_input: AfterToolHookInput) -> HookResult:
        if hook_input.tool_name != "run_shell_command":
            return HookResult.no_changes()

        tool_input = hook_input.tool_input if isinstance(hook_input.tool_input, dict) else {}
        command = str(tool_input.get("command", ""))
        is_background = bool(
            tool_input.get("is_background")
            or tool_input.get("background")
            or tool_input.get("is_background_command")
        )
        timeout_ms = tool_input.get("timeout_ms")
        has_timeout_ms = timeout_ms is not None or bool(TIMEOUT_MS_RE.search(str(tool_input)))
        try:
            timeout_ms_i = int(timeout_ms) if timeout_ms is not None else None
        except (TypeError, ValueError):
            timeout_ms_i = None

        state = self._load_state(hook_input.agent_state.get_global_value(STATE_KEY, {}))
        seen = set(state.get("seen_env_probes") or [])

        if SHORT_PROBE_RE.search(command) and not LONG_WORK_RE.search(command):
            state["short_probe_events"] = int(state["short_probe_events"]) + 1
            state["last_reason"] = "short_probe_ok"

        if ENV_PROBE_RE.search(command):
            key = re.sub(r"\s+", " ", command.strip())[:120]
            if key in seen:
                state["env_probe_events"] = int(state["env_probe_events"]) + 1
                state["last_reason"] = "env_reprobe_loop"
            else:
                seen.add(key)
                state["env_probe_events"] = int(state["env_probe_events"]) + 1
                state["last_reason"] = "env_probe"

        if SLEEP_POLL_RE.search(command):
            state["sleep_poll_events"] = int(state["sleep_poll_events"]) + 1
            state["last_reason"] = "sleep_poll_loop"

        if LONG_WORK_RE.search(command):
            state["long_work_events"] = int(state["long_work_events"]) + 1
            if is_background:
                state["bg_long_events"] = int(state["bg_long_events"]) + 1
                state["last_reason"] = "long_work_background"
            else:
                # Foreground long work without an explicit short timeout is the
                # core §6 miss (default shell timeout is often 5 minutes).
                if not has_timeout_ms or (
                    timeout_ms_i is not None and timeout_ms_i >= self.long_step_ms
                ):
                    state["long_fg_no_timeout"] = int(state["long_fg_no_timeout"]) + 1
                    state["last_reason"] = "long_foreground_without_short_timeout"
                else:
                    state["last_reason"] = "long_foreground_with_timeout_ms"

        # Duration heuristic from tool result when available.
        duration_ms = self._tool_duration_ms(hook_input)
        if (
            duration_ms is not None
            and duration_ms >= self.long_step_ms
            and not is_background
            and not has_timeout_ms
        ):
            state["long_fg_no_timeout"] = int(state["long_fg_no_timeout"]) + 1
            state["last_reason"] = "long_foreground_wallclock"

        state["seen_env_probes"] = sorted(seen)
        hook_input.agent_state.set_global_value(STATE_KEY, self._dump_state(state))
        return HookResult.no_changes()

    def before_model(self, hook_input: BeforeModelHookInput) -> HookResult:
        iteration = int(hook_input.current_iteration or 0)
        state = self._load_state(hook_input.agent_state.get_global_value(STATE_KEY, {}))
        sticky = str(state.get("sticky_reminder") or "")
        messages = list(hook_input.messages)
        sticky_applied = False

        # Tess TWEAK `0556`: sticky must re-paint SYSTEM even when a mid-run USER
        # append still holds the marker — cleaned InMemoryTracer often drops
        # those USER appends and freezes `system_prompt` from the SYSTEM surface.
        if sticky and not self._system_has_marker(messages, MARKER):
            messages, sys_ok = self._apply_system_marker(messages, sticky)
            messages, user_ok = self._apply_user_marker(messages, sticky)
            sticky_applied = sys_ok or user_ok
            if sticky_applied:
                logger.info(
                    "[ShortProbeFirstGateMiddleware] Sticky re-apply "
                    "iteration=%s system=%s user=%s",
                    iteration,
                    sys_ok,
                    user_ok,
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

        short = int(state["short_probe_events"])
        long_work = int(state["long_work_events"])
        long_fg = int(state["long_fg_no_timeout"])
        env_probe = int(state["env_probe_events"])
        sleep_poll = int(state["sleep_poll_events"])
        bg_long = int(state["bg_long_events"])

        fire = False
        reason = state.get("last_reason") or ""

        # 1) Long work before any short probe.
        if long_work > 0 and short == 0:
            fire = True
            reason = "long_work_before_short_probe"
        # 2) Foreground long work / long wallclock without timeout_ms hygiene.
        elif long_fg > 0 and bg_long == 0:
            fire = True
            reason = reason or "long_foreground_without_short_timeout"
        # 3) Env re-probe loop (same class of probe repeated).
        elif env_probe >= self.min_env_probe_repeats and short <= 1:
            fire = True
            reason = "env_reprobe_without_pivot"
        # 4) Sleep/poll burning budget.
        elif sleep_poll > 0 and long_work > 0:
            fire = True
            reason = "sleep_poll_during_long_work"

        if not fire:
            return _passthrough()

        state["nudge_count"] += 1
        state["last_nudge_iteration"] = iteration
        state["nudge_fired"] = True

        reminder = (
            f"{MARKER} {reason}; iteration={iteration}; "
            f"short_probe_events={short}; long_work_events={long_work}; "
            f"long_fg_no_timeout={long_fg}; env_probe_events={env_probe}; "
            f"sleep_poll_events={sleep_poll}; bg_long_events={bg_long}. "
            "Manage time explicitly: prefer short probes first; set "
            "`timeout_ms` on risky foreground probes (parameter name is "
            "`timeout_ms`, not `timeout`); put long installs/trains/servers "
            "in `is_background: true` and inspect with short follow-ups; "
            "confirm a missing dependency once, then stop re-probing the "
            "environment and pivot. This is not a near-deadline publish "
            "order and not a request to invent domain tips."
        )
        self._write_sidecar(reminder)
        state["sticky_reminder"] = reminder
        hook_input.agent_state.set_global_value(STATE_KEY, self._dump_state(state))

        # Tess TWEAK `0556`: SYSTEM PREPEND (else INSERT) + first-USER PREPEND +
        # sticky re-paint + trailing USER + sidecar. Also FRAMEWORK dual-land
        # (LocalContext/OfficialChecker) for the live model path.
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
            "[ShortProbeFirstGateMiddleware] Nudge #%s reason=%s iteration=%s "
            "short=%s long=%s long_fg=%s env=%s system_patched=%s user_patched=%s",
            state["nudge_count"],
            reason,
            iteration,
            short,
            long_work,
            long_fg,
            env_probe,
            system_patched,
            user_patched,
        )
        return HookResult.with_modifications(messages=updated_messages)

    def _tool_duration_ms(self, hook_input: AfterToolHookInput) -> int | None:
        for attr in ("duration_ms", "elapsed_ms", "tool_duration_ms"):
            raw = getattr(hook_input, attr, None)
            if raw is None and isinstance(getattr(hook_input, "tool_result", None), dict):
                raw = hook_input.tool_result.get(attr)
            try:
                if raw is not None:
                    return int(raw)
            except (TypeError, ValueError):
                continue
        return None

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
                    "[ShortProbeFirstGateMiddleware] Appended fire sidecar %s",
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
            "short_probe_events": int(state.get("short_probe_events", 0) or 0),
            "long_work_events": int(state.get("long_work_events", 0) or 0),
            "long_fg_no_timeout": int(state.get("long_fg_no_timeout", 0) or 0),
            "env_probe_events": int(state.get("env_probe_events", 0) or 0),
            "sleep_poll_events": int(state.get("sleep_poll_events", 0) or 0),
            "bg_long_events": int(state.get("bg_long_events", 0) or 0),
            "nudge_count": int(state.get("nudge_count", 0) or 0),
            "last_nudge_iteration": int(
                state.get("last_nudge_iteration", -10_000) or -10_000
            ),
            "nudge_fired": bool(state.get("nudge_fired", False)),
            "last_reason": str(state.get("last_reason") or ""),
            "sticky_reminder": str(state.get("sticky_reminder") or ""),
            "seen_env_probes": sorted(
                {str(p) for p in (state.get("seen_env_probes") or []) if p}
            ),
        }

    def _load_state(self, raw_state: Any) -> dict[str, Any]:
        return self._dump_state(raw_state if isinstance(raw_state, dict) else {})
