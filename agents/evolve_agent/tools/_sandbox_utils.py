"""
Sandbox helpers for gemini-cli style builtin tools.

These tool implementations route all filesystem and shell operations through
NexAU's BaseSandbox abstraction (e.g. LocalSandbox/E2B).
"""

from __future__ import annotations

import os
from pathlib import Path

from nexau.archs.main_sub.agent_state import AgentState
from nexau.archs.sandbox import BaseSandbox
from nexau.archs.sandbox.base_sandbox import SandboxError


def get_sandbox(agent_state: AgentState | None) -> BaseSandbox:
    """Get sandbox from agent_state, or raise SandboxError."""
    if agent_state is not None:
        sandbox = agent_state.get_sandbox()
        if sandbox is not None:
            return sandbox
    raise SandboxError("Sandbox not found")


_POLICY_ENV = "AHE_REWARD_ONLY_POLICY"
_READ_ROOTS_ENV = "AHE_TOOL_READ_ROOTS"
_WRITE_ROOTS_ENV = "AHE_TOOL_WRITE_ROOTS"


def reward_only_policy_enabled() -> bool:
    return os.environ.get(_POLICY_ENV, "").strip() == "1"


def _policy_roots(env_name: str) -> list[Path]:
    raw = os.environ.get(env_name, "")
    return [Path(item).resolve() for item in raw.split(os.pathsep) if item.strip()]


def _is_within(path: Path, roots: list[Path]) -> bool:
    for root in roots:
        try:
            path.relative_to(root)
            return True
        except ValueError:
            continue
    return False


def resolve_path(path: str, sandbox: BaseSandbox, *, access: str = "read") -> str:
    """Resolve a tool path and enforce reward-only read/write roots.

    The resolved real path is checked, so absolute paths, ``..`` traversal, and
    symlink escapes are all rejected. Outside reward-only runs this preserves
    the historical unrestricted LocalSandbox behavior.
    """
    candidate = Path(path)
    if not candidate.is_absolute():
        candidate = Path(str(sandbox.work_dir)) / candidate
    if not reward_only_policy_enabled():
        return str(candidate)

    resolved = candidate.resolve(strict=False)
    if access not in {"read", "write"}:
        raise SandboxError(f"Unsupported path access mode: {access}")
    env_name = _WRITE_ROOTS_ENV if access == "write" else _READ_ROOTS_ENV
    roots = _policy_roots(env_name)
    if not roots:
        raise SandboxError(f"Reward-only path policy has no {access} roots configured")
    if not _is_within(resolved, roots):
        raise SandboxError(f"Reward-only path policy denied {access} access")
    return str(resolved)
