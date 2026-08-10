import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from nexau.archs.sandbox.base_sandbox import SandboxError


EVOLVE_AGENT_DIR = Path(__file__).resolve().parents[1] / "agents" / "evolve_agent"
sys.path.insert(0, str(EVOLVE_AGENT_DIR))

from tools._sandbox_utils import resolve_path  # noqa: E402


def test_reward_only_path_policy_rejects_absolute_parent_and_symlink_escape(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    analysis = tmp_path / "analysis"
    outside = tmp_path / "raw-harbor"
    workspace.mkdir()
    analysis.mkdir()
    outside.mkdir()
    (outside / "result.json").write_text("forbidden", encoding="utf-8")
    (workspace / "escape").symlink_to(outside, target_is_directory=True)

    monkeypatch.setenv("AHE_REWARD_ONLY_POLICY", "1")
    monkeypatch.setenv("AHE_TOOL_READ_ROOTS", os.pathsep.join([str(workspace), str(analysis)]))
    monkeypatch.setenv("AHE_TOOL_WRITE_ROOTS", str(workspace))
    sandbox = SimpleNamespace(work_dir=tmp_path)

    assert resolve_path("workspace/new.py", sandbox, access="write") == str(workspace / "new.py")
    assert resolve_path("analysis/overview.md", sandbox) == str(analysis / "overview.md")
    with pytest.raises(SandboxError):
        resolve_path(str(outside / "result.json"), sandbox)
    with pytest.raises(SandboxError):
        resolve_path("workspace/../raw-harbor/result.json", sandbox)
    with pytest.raises(SandboxError):
        resolve_path("workspace/escape/result.json", sandbox)
    with pytest.raises(SandboxError):
        resolve_path("analysis/overview.md", sandbox, access="write")


def test_reward_only_agent_configs_remove_arbitrary_shell_and_debugger_writes() -> None:
    evolve_config = yaml.safe_load((EVOLVE_AGENT_DIR / "evolve_agent.yaml").read_text(encoding="utf-8"))
    evolve_tools = {tool["name"] for tool in evolve_config["tools"]}
    assert "run_shell_command" not in evolve_tools
    assert "inspect_workspace" in evolve_tools

    debugger_config_path = (
        EVOLVE_AGENT_DIR
        / "skills/agent-debugger-cli/_source/agent_debugger_core/runtime/agent_config.yaml"
    )
    debugger_config = yaml.safe_load(debugger_config_path.read_text(encoding="utf-8"))
    assert {tool["name"] for tool in debugger_config["tools"]} == {"read_file", "complete_task"}

    tool_schema = yaml.safe_load(
        (EVOLVE_AGENT_DIR / "tool_descriptions/inspect_workspace.tool.yaml").read_text(encoding="utf-8")
    )
    assert "command" not in tool_schema["input_schema"]["properties"]
    assert tool_schema["input_schema"]["additionalProperties"] is False
