"""Inspect and validate only the configured harness workspace.

This deliberately exposes no arbitrary command argument. It replaces the
general-purpose shell during reward-only evolution so evaluator artifacts
outside the workspace cannot be reached through command composition.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import yaml

from nexau.archs.main_sub.agent_state import AgentState

from tools._sandbox_utils import get_sandbox, resolve_path


_MAX_OUTPUT_CHARS = 40_000


def _git(workspace: Path, *args: str) -> tuple[int, str]:
    result = subprocess.run(
        ["git", *args],
        cwd=workspace,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    output = result.stdout
    if result.stderr:
        output = f"{output}\n{result.stderr}" if output else result.stderr
    if len(output) > _MAX_OUTPUT_CHARS:
        output = output[:20_000] + "\n... [truncated] ...\n" + output[-20_000:]
    return result.returncode, output.strip()


def _validate(workspace: Path) -> tuple[bool, str]:
    failures: list[str] = []
    checked_python = 0
    for path in sorted(workspace.rglob("*.py")):
        if ".git" in path.parts or "__pycache__" in path.parts:
            continue
        try:
            compile(path.read_text(encoding="utf-8"), str(path), "exec")
            checked_python += 1
        except (OSError, SyntaxError, UnicodeError) as exc:
            failures.append(f"python syntax: {path.relative_to(workspace)}: {exc}")

    config_path = workspace / "code_agent.yaml"
    try:
        parsed = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        if not isinstance(parsed, dict):
            failures.append("code_agent.yaml is not a mapping")
    except (OSError, yaml.YAMLError) as exc:
        failures.append(f"code_agent.yaml: {exc}")

    diff_rc, diff_output = _git(workspace, "diff", "--check")
    if diff_rc != 0:
        failures.append(f"git diff --check:\n{diff_output}")

    if failures:
        return False, "\n".join(failures)
    return True, f"PASS: parsed code_agent.yaml; compiled {checked_python} Python files; git diff --check passed"


def inspect_workspace(
    action: str,
    agent_state: AgentState | None = None,
) -> dict[str, Any]:
    """Run one fixed, non-shell workspace inspection action."""
    try:
        sandbox = get_sandbox(agent_state)
        workspace = Path(resolve_path("workspace", sandbox, access="write"))
        if not workspace.is_dir():
            raise ValueError("Configured workspace directory does not exist")

        if action == "status":
            rc, output = _git(workspace, "status", "--short", "--branch")
            ok = rc == 0
        elif action == "diff":
            rc, output = _git(workspace, "diff", "--")
            ok = rc == 0
        elif action == "validate":
            ok, output = _validate(workspace)
        else:
            return {
                "content": f"Unsupported action: {action}",
                "returnDisplay": "Error: Unsupported action",
                "error": {"message": "Unsupported action", "type": "INVALID_ACTION"},
            }

        result: dict[str, Any] = {
            "content": output or "(empty)",
            "returnDisplay": output or "(empty)",
        }
        if not ok:
            result["error"] = {"message": "Workspace inspection failed", "type": "VALIDATION_FAILED"}
        return result
    except Exception as exc:
        return {
            "content": f"Workspace inspection error: {exc}",
            "returnDisplay": "Error: Workspace inspection failed",
            "error": {"message": str(exc), "type": "WORKSPACE_INSPECTION_ERROR"},
        }
