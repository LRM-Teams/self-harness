from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace

from nexau.archs.main_sub.execution.hooks import AfterToolHookInput, BeforeToolHookInput


ROOT = Path(__file__).resolve().parents[1]
GUARD_PATH = (
    ROOT
    / "harness_inputs"
    / "cycle6-input-20260811-091756"
    / "middleware"
    / "execution_guard.py"
)
SPEC = importlib.util.spec_from_file_location("cycle5_continuation_execution_guard", GUARD_PATH)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)
ExecutionGuardMiddleware = MODULE.ExecutionGuardMiddleware


def _before(command: str, *, run_id: str = "run") -> BeforeToolHookInput:
    return BeforeToolHookInput(
        agent_state=SimpleNamespace(run_id=run_id),
        sandbox=None,
        tool_name="run_shell_command",
        tool_call_id="call",
        tool_input={"command": command},
    )


def _after(command: str, content: str = "ok", *, run_id: str = "run") -> AfterToolHookInput:
    return AfterToolHookInput(
        agent_state=SimpleNamespace(run_id=run_id),
        sandbox=None,
        tool_name="run_shell_command",
        tool_call_id="call",
        tool_input={"command": command},
        tool_output={"content": content, "exit_code": 0},
    )


def test_artifact_detection_requires_an_actual_write() -> None:
    guard = ExecutionGuardMiddleware()

    assert guard._artifact_write_paths("printf 'candidate /app/results.json'") == set()
    assert guard._artifact_write_paths("cp /app/results.json /tmp/backup.json") == set()
    assert guard._artifact_write_paths("printf ok > /app/results.json") == {
        "/app/results.json"
    }
    assert guard._artifact_write_paths("Path('/app/out.txt').write_text(value)") == {
        "/app/out.txt"
    }


def test_uncertainty_gate_ignores_path_mentions_but_tracks_candidate_writes() -> None:
    guard = ExecutionGuardMiddleware()
    run_key = "run_id:run"
    guard._required_paths[run_key] = {"/app/results.json"}

    guard.after_tool(_after("printf 'candidate /app/results.json'"))
    assert run_key not in guard._uncertain_output_runs

    guard.after_tool(_after("printf candidate > /app/results.json"))
    assert run_key in guard._uncertain_output_runs


def test_placeholder_write_to_required_path_is_blocked() -> None:
    guard = ExecutionGuardMiddleware()
    guard._required_paths["run_id:run"] = {"/app/results.json"}

    result = guard.before_tool(_before("printf PLACEHOLDER > /app/results.json"))

    assert result.tool_input is not None
    assert "blocked placeholder content" in result.tool_input["command"]
    assert "exit 64" in result.tool_input["command"]


def test_sqlite_recovery_backs_up_unique_evidence_before_first_open() -> None:
    guard = ExecutionGuardMiddleware()
    guard._source_protection_runs.add("run_id:run")

    result = guard.before_tool(_before("sqlite3 /app/recovery.db '.tables'"))

    assert result.tool_input is not None
    command = result.tool_input["command"]
    assert command.index("ahe-source-backup") < command.index("sqlite3 /app/recovery.db")
    assert "*.db-*" in command
    assert "*wal*" in command
    assert "AHE_SOURCE_BACKUP_OK" in command


def test_scoped_gate_covers_exact_schema_interface_and_numeric_threshold() -> None:
    guard = ExecutionGuardMiddleware()
    guard._user_text["run_id:run"] = (
        "Implement a gRPC endpoint with a message named Lookup and exact field names, "
        "then reach at least 0.62 accuracy."
    )

    recovery = guard._scoped_final_recovery("run_id:run")

    assert "exact client-facing" in recovery
    assert "method, message, field" in recovery
    assert "independently written client/parser" in recovery
    assert "required numeric threshold" in recovery
    assert "safety margin" in recovery


def test_complex_tasks_get_more_room_before_progress_warning() -> None:
    guard = ExecutionGuardMiddleware(progress_warning_shell_calls=2)
    guard._complex_runs.add("run_id:run")

    outputs = [guard.after_tool(_after("true", f"ok-{index}")) for index in range(1, 5)]

    second = outputs[1].tool_output or {}
    fourth = outputs[3].tool_output or {}
    assert "used 2 shell commands" not in str(second.get("content", ""))
    assert "used 4 shell commands" in str(fourth.get("content", ""))
