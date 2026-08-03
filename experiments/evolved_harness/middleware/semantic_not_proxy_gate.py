"""Semantic-not-proxy gate — AHE §8 / §1 portable point.

When a contract deliverable has been written (or only weakly checked via
existence/size/import), soft-nudge for an evaluator-facing *semantic* check on
the on-disk artifact. Does NOT leak task-domain recipes — only process pressure
against treating file presence as acceptance.
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

STATE_KEY = "semantic_not_proxy_gate_state"
logger = logging.getLogger(__name__)

DELIVERABLE_PATH_RE = re.compile(r"(/app/[A-Za-z0-9_./-]+)")
WRITE_CMD_RE = re.compile(
    r"(?:>|tee\b|cp\b|mv\b|install\b|write_text|open\([^)]*['\"]w|"
    r"\.save\(|torch\.save|pickle\.dump|joblib\.dump|net\.save|"
    r"caffemodel|to_json|json\.dump)",
    re.IGNORECASE,
)
REQUIRED_HINT_RE = re.compile(
    r"(?:stored in|write(?:n)? to|save(?:d)? (?:to|as)|output(?: file)?|file titled|"
    r"JSON file called|save your solution in the file|/app/)"
    r"[^\n]{0,60}?(/app/[A-Za-z0-9_./-]+)",
    re.IGNORECASE,
)
# Weak proxies: existence / size / import success ≠ acceptance (AHE §8).
WEAK_PROXY_RE = re.compile(
    r"(?:test\s+-[ef]|\[[^\]]*-[ef][^\]]*\]|\bls\b|\bstat\b|\bdu\b|\bfile\b|"
    r"\bwc\s+-c\b|\bwc\s+-l\b|\bos\.path\.exists\b|\bos\.path\.getsize\b|"
    r"\bPath\([^\)]*\)\.exists\(|\bPath\([^\)]*\)\.stat\(|"
    r"\bimport\s+[A-Za-z_]|\bpython3?\s+-c\s+['\"]import\b)",
    re.IGNORECASE,
)
# Semantic / content-level evidence (structure, loadability, metrics, schema).
SEMANTIC_EVIDENCE_RE = re.compile(
    r"(?:"
    r"\bpytest\b|"
    r"\bpython3?\s+[^\n]*(?:check|eval|verify|validate|test_outputs)\.py\b|"
    r"\btorch\.load\b|\bpickle\.load\b|\bjoblib\.load\b|\bnp\.load\b|\bnumpy\.load\b|"
    r"\bjson\.load\b|\byaml\.safe_load\b|\bh5py\.File\b|"
    r"\bnet\.load|caffemodel|\bBlobProto\b|"
    r"\bassert\b|\braise\s+AssertionError\b|"
    r"\b\.shape\b|\b\.ndim\b|\bstate_dict\b|\bnum_parameters\b|"
    r"\bsha256sum\b|\bmd5sum\b|"
    r"\bMeltingTemp\b|\bTm\b|\bmedian\b|\bpercentile\b|"
    r"schema|required[_ ]keys|validate_schema|"
    r"evaluator-facing|semantic\s+check"
    r")",
    re.IGNORECASE,
)

_SIDECAR_CANDIDATES = (
    Path("/logs/agent/semantic_not_proxy.nudge.txt"),
    Path("semantic_not_proxy.nudge.txt"),
)


class SemanticNotProxyGateMiddleware(Middleware):
    """Require semantic/content checks; reject existence/size/import proxies."""

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
        for message in hook_input.messages:
            if getattr(message, "role", None) != Role.USER:
                continue
            text = self._message_text(message)
            required.update(DELIVERABLE_PATH_RE.findall(text))
            for match in REQUIRED_HINT_RE.finditer(text):
                required.add(match.group(1).rstrip(".,;)'\""))

        state["required_paths"] = required
        state["written_paths"] = set()
        state["weak_proxy_count"] = 0
        state["semantic_evidence"] = 0
        state["nudge_count"] = 0
        state["last_nudge_iteration"] = -10_000
        state["nudge_fired"] = False
        hook_input.agent_state.set_global_value(STATE_KEY, self._dump_state(state))
        if required:
            logger.info(
                "[SemanticNotProxyGateMiddleware] required=%s",
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

        if WEAK_PROXY_RE.search(command):
            state["weak_proxy_count"] = int(state.get("weak_proxy_count", 0) or 0) + 1
        if SEMANTIC_EVIDENCE_RE.search(command):
            state["semantic_evidence"] = int(state.get("semantic_evidence", 0) or 0) + 1

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
        if not written:
            return HookResult.no_changes()

        semantic = int(state.get("semantic_evidence", 0) or 0)
        if semantic >= 1:
            return HookResult.no_changes()

        weak = int(state.get("weak_proxy_count", 0) or 0)
        reason = (
            f"deliverable_written_semantic_evidence=0"
            f"_weak_proxy_count={weak}"
        )
        state["nudge_count"] += 1
        state["last_nudge_iteration"] = iteration
        state["nudge_fired"] = True
        hook_input.agent_state.set_global_value(STATE_KEY, self._dump_state(state))

        paths = ", ".join(sorted(state["required_paths"] or state["written_paths"])[:4]) or (
            "/app/<required>"
        )
        reminder = (
            f"SemanticNotProxyGate: {reason}; iteration={iteration}. "
            f"Contract path(s): {paths}. "
            "File existence, byte-count, `ls`/`stat`, or bare `import` success "
            "are **not** acceptance. Load the published on-disk artifact and run "
            "an evaluator-facing **semantic/content** check (structure, loadable "
            "params, schema/keys, or the contract's real metric) before you stop. "
            "If that check fails, fix the artifact — do not invent a weaker proxy. "
            "Do not invent domain tips from this reminder — only semantic verification."
        )
        self._write_sidecar(reminder)
        # Dual-land: SYSTEM append (cleaned `system_prompt` marker) + USER + sidecar.
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
            "[SemanticNotProxyGateMiddleware] Nudge #%s reason=%s iteration=%s "
            "weak=%s semantic=%s system_patched=%s",
            state["nudge_count"],
            reason,
            iteration,
            weak,
            semantic,
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
                    "[SemanticNotProxyGateMiddleware] Appended fire sidecar %s", path
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
            "written_paths": _as_set(state.get("written_paths")),
            "weak_proxy_count": int(state.get("weak_proxy_count", 0) or 0),
            "semantic_evidence": int(state.get("semantic_evidence", 0) or 0),
            "nudge_count": int(state.get("nudge_count", 0) or 0),
            "last_nudge_iteration": int(
                state.get("last_nudge_iteration", -10_000) or -10_000
            ),
            "nudge_fired": bool(state.get("nudge_fired", False)),
        }

    def _dump_state(self, state: dict[str, Any]) -> dict[str, Any]:
        return {
            "required_paths": sorted(state.get("required_paths", set())),
            "written_paths": sorted(state.get("written_paths", set())),
            "weak_proxy_count": int(state.get("weak_proxy_count", 0) or 0),
            "semantic_evidence": int(state.get("semantic_evidence", 0) or 0),
            "nudge_count": int(state.get("nudge_count", 0) or 0),
            "last_nudge_iteration": int(
                state.get("last_nudge_iteration", -10_000) or -10_000
            ),
            "nudge_fired": bool(state.get("nudge_fired", False)),
        }
