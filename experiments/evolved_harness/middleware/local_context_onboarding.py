"""Inject a one-shot local-context onboarding nudge to cut blind env probes."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from nexau.archs.main_sub.execution.hooks import (
    BeforeAgentHookInput,
    BeforeModelHookInput,
    HookResult,
    Middleware,
)
from nexau.core.messages import Message, Role, TextBlock

STATE_KEY = "local_context_onboarding_state"
logger = logging.getLogger(__name__)

ONBOARDING_TEXT = (
    "LocalContext onboarding: before broad exploration, run ONE short probe batch "
    "and lock the facts (then stop re-probing the same questions):\n"
    "1) `pwd`; `ls -la`; note `/app` vs cwd and required deliverable paths from the contract.\n"
    "2) Toolchain once: `python3 -V`; `node -v 2>/dev/null || true`; "
    "`qemu-system-x86_64 --version 2>/dev/null | head -1 || true`; "
    "`which nginx qemu-system-x86_64 2>/dev/null || true`.\n"
    "3) If the task needs a background service/VM, start it once with explicit ports/"
    "display args from the contract; poll readiness with short commands; leave it running.\n"
    "4) Do not spend iterations re-checking versions, re-installing the same packages, or "
    "re-discovering paths already established. Pivot immediately to contract deliverables "
    "and evaluator-style checks."
)

# Sidecar paths so attribution does not depend solely on FRAMEWORK messages
# (cleaned tracers omit FRAMEWORK; system_prompt + this file are the durable hits).
_SIDECAR_CANDIDATES = (
    Path("/logs/agent/local_context_onboarding.nudge.txt"),
    Path("local_context_onboarding.nudge.txt"),
)


class LocalContextOnboardingMiddleware(Middleware):
    """One-shot env/context onboarding to reduce timeout waste from blind probes.

    Targets tasks that burn budget on repeated `which`/`--version`/path discovery
    (qemu/service/toolchain clusters) before touching the real contract.

    Trace landing (Tess TWEAK `0025`): cleaned InMemoryTracer drops FRAMEWORK
    messages, so a FRAMEWORK-only inject never yields literal `LocalContext
    onboarding:` hits. We therefore also prepend the same text onto the first
    SYSTEM message (survives as `system_prompt` in cleaned.json) and write a
    sidecar marker under `/logs/agent/`.
    """

    def __init__(self, *, inject_before_first_assistant: bool = True) -> None:
        self.inject_before_first_assistant = inject_before_first_assistant

    def before_agent(self, hook_input: BeforeAgentHookInput) -> HookResult:
        hook_input.agent_state.set_global_value(
            STATE_KEY,
            {"injected": False, "nudge_count": 0},
        )
        return HookResult.no_changes()

    def before_model(self, hook_input: BeforeModelHookInput) -> HookResult:
        state = self._load_state(hook_input.agent_state.get_global_value(STATE_KEY, {}))
        if state["injected"]:
            return HookResult.no_changes()

        has_assistant = any(
            getattr(message, "role", None) == Role.ASSISTANT for message in hook_input.messages
        )
        if has_assistant and self.inject_before_first_assistant:
            # Late init / resumed turns: still inject once if somehow missed.
            pass

        state["injected"] = True
        state["nudge_count"] = 1
        hook_input.agent_state.set_global_value(STATE_KEY, state)

        updated_messages: list[Message] = []
        system_patched = False
        for message in hook_input.messages:
            if (
                not system_patched
                and getattr(message, "role", None) == Role.SYSTEM
            ):
                existing = message.get_text_content() or ""
                if ONBOARDING_TEXT not in existing:
                    patched = ONBOARDING_TEXT + "\n\n" + existing
                    updated_messages.append(
                        Message(role=Role.SYSTEM, content=[TextBlock(text=patched)])
                    )
                else:
                    updated_messages.append(message)
                system_patched = True
            else:
                updated_messages.append(message)

        # Keep FRAMEWORK inject for the model (adapter maps FRAMEWORK → user).
        updated_messages.append(
            Message(role=Role.FRAMEWORK, content=[TextBlock(text=ONBOARDING_TEXT)])
        )
        self._write_sidecar()
        logger.info(
            "[LocalContextOnboardingMiddleware] Injected onboarding nudge iteration=%s "
            "system_patched=%s",
            hook_input.current_iteration,
            system_patched,
        )
        return HookResult.with_modifications(messages=updated_messages)

    def _write_sidecar(self) -> None:
        for path in _SIDECAR_CANDIDATES:
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(ONBOARDING_TEXT + "\n", encoding="utf-8")
                logger.info(
                    "[LocalContextOnboardingMiddleware] Wrote nudge sidecar %s", path
                )
                return
            except OSError as exc:
                logger.debug(
                    "[LocalContextOnboardingMiddleware] Sidecar write failed %s: %s",
                    path,
                    exc,
                )

    def _load_state(self, raw_state: Any) -> dict[str, Any]:
        state = raw_state if isinstance(raw_state, dict) else {}
        return {
            "injected": bool(state.get("injected", False)),
            "nudge_count": int(state.get("nudge_count", 0) or 0),
        }
