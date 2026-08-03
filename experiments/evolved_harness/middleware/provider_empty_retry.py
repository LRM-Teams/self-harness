"""Provider-empty outer retry — timeout/fail knob P0-1 (post-`0112`).

LLMCaller already retries empty responses a few times. On `0112` regex-chess
still saw dozens of `No response content or tool calls` and never wrote
``/app/re.json``. This middleware adds an *outer* backoff retry around
``wrap_model_call`` and a light continue-nudge after a streak, without
changing task answers or raising whole-ticket timeouts.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any

from nexau.archs.main_sub.execution.hooks import (
    BeforeAgentHookInput,
    BeforeModelHookInput,
    HookResult,
    Middleware,
    ModelCallFn,
    ModelCallParams,
)
from nexau.archs.main_sub.execution.model_response import ModelResponse
from nexau.core.messages import Message, Role, TextBlock

STATE_KEY = "provider_empty_retry_state"
MARKER = "ProviderEmptyRetry:"
EMPTY_MARKERS = (
    "No response content or tool calls",
    "no response content or tool calls",
)
logger = logging.getLogger(__name__)


def _is_empty_response_error(exc: BaseException) -> bool:
    text = f"{type(exc).__name__}: {exc}"
    return any(m in text for m in EMPTY_MARKERS)


class ProviderEmptyRetryMiddleware(Middleware):
    """Outer backoff retry when the provider returns an empty completion."""

    def __init__(
        self,
        *,
        max_outer_retries: int = 3,
        initial_backoff_sec: float = 2.0,
        backoff_multiplier: float = 2.0,
        max_backoff_sec: float = 16.0,
        streak_nudge_threshold: int = 2,
        max_nudges: int = 3,
        renudge_every_iterations: int = 6,
    ) -> None:
        self.max_outer_retries = max(0, int(max_outer_retries))
        self.initial_backoff_sec = float(initial_backoff_sec)
        self.backoff_multiplier = float(backoff_multiplier)
        self.max_backoff_sec = float(max_backoff_sec)
        self.streak_nudge_threshold = max(1, int(streak_nudge_threshold))
        self.max_nudges = max(0, int(max_nudges))
        self.renudge_every_iterations = max(1, int(renudge_every_iterations))

    def before_agent(self, hook_input: BeforeAgentHookInput) -> HookResult:
        fresh = self._fresh_state()
        _PROCESS_STATE[id(self)] = dict(fresh)
        hook_input.agent_state.set_global_value(STATE_KEY, fresh)
        return HookResult.no_changes()

    def wrap_model_call(
        self, params: ModelCallParams, call_next: ModelCallFn
    ) -> ModelResponse | None:
        attempt = 0
        backoff = self.initial_backoff_sec
        last_exc: BaseException | None = None

        while True:
            try:
                result = call_next(params)
                if attempt > 0:
                    self._record_recovery(attempt)
                else:
                    self._clear_consecutive()
                return result
            except Exception as exc:
                last_exc = exc
                if not _is_empty_response_error(exc):
                    raise
                self._record_empty(exc, attempt)
                if attempt >= self.max_outer_retries:
                    logger.error(
                        "[ProviderEmptyRetryMiddleware] Exhausted outer retries "
                        "(%s); re-raising empty response",
                        self.max_outer_retries,
                    )
                    raise
                sleep_for = min(backoff, self.max_backoff_sec)
                logger.warning(
                    "[ProviderEmptyRetryMiddleware] Empty response "
                    "(outer %s/%s); sleep %.1fs then retry",
                    attempt + 1,
                    self.max_outer_retries,
                    sleep_for,
                )
                time.sleep(sleep_for)
                backoff = min(backoff * self.backoff_multiplier, self.max_backoff_sec)
                attempt += 1

        if last_exc is not None:
            raise last_exc
        raise RuntimeError("ProviderEmptyRetry: unreachable")

    def before_model(self, hook_input: BeforeModelHookInput) -> HookResult:
        state = self._load_state(hook_input.agent_state.get_global_value(STATE_KEY, {}))
        if state["nudge_count"] >= self.max_nudges:
            return HookResult.no_changes()
        if int(state["consecutive_empty"]) < self.streak_nudge_threshold:
            return HookResult.no_changes()

        iteration = hook_input.current_iteration
        if (
            state["nudge_count"] > 0
            and iteration - state["last_nudge_iteration"] < self.renudge_every_iterations
        ):
            return HookResult.no_changes()

        reminder = (
            f"{MARKER} provider returned empty completions "
            f"(streak={state['consecutive_empty']}, empties={state['empty_events']}, "
            f"recoveries={state['recoveries']}). Do not restart from scratch — "
            "continue from the last successful tool result; write required "
            "deliverables under /app as soon as you can. Do not invent task answers."
        )
        self._write_sidecar(reminder)
        state["nudge_count"] = int(state["nudge_count"]) + 1
        state["last_nudge_iteration"] = iteration
        state["nudge_fired"] = True
        state["last_reason"] = "empty_streak_continue_nudge"
        state["consecutive_empty"] = 0
        hook_input.agent_state.set_global_value(STATE_KEY, state)

        messages = list(hook_input.messages)
        messages.append(Message(role=Role.FRAMEWORK, content=[TextBlock(text=reminder)]))
        messages.append(Message(role=Role.USER, content=[TextBlock(text=reminder)]))
        logger.info(
            "[ProviderEmptyRetryMiddleware] Continue-nudge iteration=%s count=%s",
            iteration,
            state["nudge_count"],
        )
        return HookResult.with_modifications(messages=messages)

    def _record_empty(self, exc: BaseException, attempt: int) -> None:
        # Best-effort: state lives on agent; wrap_model_call has no hook_input.
        # Persist fire evidence via sidecar; in-memory streak updated in before_model
        # via a process-local cache keyed by thread.
        key = id(self)
        cache = _PROCESS_STATE.setdefault(key, self._fresh_state())
        cache["empty_events"] = int(cache.get("empty_events", 0)) + 1
        cache["consecutive_empty"] = int(cache.get("consecutive_empty", 0)) + 1
        cache["last_reason"] = f"empty_outer_attempt_{attempt}:{type(exc).__name__}"
        cache["nudge_fired"] = True
        self._write_sidecar(
            f"{MARKER} empty_response attempt={attempt} "
            f"streak={cache['consecutive_empty']} err={type(exc).__name__}"
        )
        _PROCESS_STATE[key] = cache

    def _record_recovery(self, attempt: int) -> None:
        key = id(self)
        cache = _PROCESS_STATE.setdefault(key, self._fresh_state())
        cache["recoveries"] = int(cache.get("recoveries", 0)) + 1
        cache["consecutive_empty"] = 0
        cache["nudge_fired"] = True
        cache["last_reason"] = f"recovered_after_outer_{attempt}"
        self._write_sidecar(
            f"{MARKER} recovered after outer_attempt={attempt} "
            f"recoveries={cache['recoveries']}"
        )
        _PROCESS_STATE[key] = cache

    def _clear_consecutive(self) -> None:
        key = id(self)
        cache = _PROCESS_STATE.get(key)
        if not cache:
            return
        cache["consecutive_empty"] = 0
        _PROCESS_STATE[key] = cache

    def _load_state(self, raw: Any) -> dict[str, Any]:
        base = self._fresh_state()
        if isinstance(raw, dict):
            base.update({k: raw.get(k, base[k]) for k in base})
        # Merge process-local wrap_model_call counters into agent state view.
        cache = _PROCESS_STATE.get(id(self), {})
        for k in ("empty_events", "recoveries", "consecutive_empty", "nudge_fired", "last_reason"):
            if k in cache:
                if k in ("empty_events", "recoveries"):
                    base[k] = max(int(base.get(k, 0)), int(cache.get(k, 0)))
                elif k == "consecutive_empty":
                    base[k] = max(int(base.get(k, 0)), int(cache.get(k, 0)))
                else:
                    base[k] = cache[k] if cache.get(k) not in (None, "") else base[k]
        return base

    @staticmethod
    def _fresh_state() -> dict[str, Any]:
        return {
            "empty_events": 0,
            "recoveries": 0,
            "consecutive_empty": 0,
            "nudge_count": 0,
            "last_nudge_iteration": -10_000,
            "nudge_fired": False,
            "last_reason": "",
        }

    def _write_sidecar(self, text: str) -> None:
        path = Path("/tmp/provider_empty_retry_nudge.txt")
        try:
            with path.open("a", encoding="utf-8") as fh:
                fh.write(text.rstrip() + "\n")
            logger.info(
                "[ProviderEmptyRetryMiddleware] Appended fire sidecar %s", path
            )
        except OSError as exc:
            logger.warning(
                "[ProviderEmptyRetryMiddleware] Sidecar write failed: %s", exc
            )


_PROCESS_STATE: dict[int, dict[str, Any]] = {}
