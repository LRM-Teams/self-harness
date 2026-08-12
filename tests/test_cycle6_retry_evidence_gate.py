from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace

from nexau.archs.main_sub.execution.hooks import AfterToolHookInput, BeforeToolHookInput


ROOT = Path(__file__).resolve().parents[1]
GUARD_PATH = (
    ROOT
    / "harness_inputs"
    / "cycle6-retry-input-20260812-061753"
    / "middleware"
    / "execution_guard.py"
)
SPEC = importlib.util.spec_from_file_location("cycle6_retry_execution_guard", GUARD_PATH)
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


def test_todo_comment_in_required_source_is_allowed() -> None:
    guard = ExecutionGuardMiddleware()
    guard._required_paths["run_id:run"] = {"/app/solution.py"}

    result = guard.before_tool(
        _before("cat > /app/solution.py <<'PY'\n# TODO: optional optimization\nprint('ready')\nPY")
    )

    assert result.tool_input is None


def test_pure_source_placeholder_is_still_blocked() -> None:
    guard = ExecutionGuardMiddleware()
    guard._required_paths["run_id:run"] = {"/app/solution.py"}

    result = guard.before_tool(_before("echo TODO > /app/solution.py"))

    assert result.tool_input is not None
    assert "blocked placeholder content" in result.tool_input["command"]


def test_schema_and_interface_gate_skips_fresh_independent_validation() -> None:
    guard = ExecutionGuardMiddleware()
    run_key = "run_id:run"
    guard._required_paths[run_key] = {"/app/schema.json"}
    guard._user_text[run_key] = (
        "Write a JSON file with exact field names and expose a gRPC endpoint."
    )

    initial = guard._scoped_final_recovery(run_key)
    assert "independently written client/parser" in initial
    assert "exact client-facing" in initial

    guard.after_tool(_after("printf '{\"ok\": true}' > /app/schema.json"))
    stale = guard._scoped_final_recovery(run_key)
    assert "independently written client/parser" in stale
    assert "exact client-facing" in stale

    guard.after_tool(_after("jq . /app/schema.json && grpcurl localhost:50051 list"))
    assert guard._scoped_final_recovery(run_key) == ""


def test_later_write_invalidates_prior_validation() -> None:
    guard = ExecutionGuardMiddleware()
    run_key = "run_id:run"
    guard._required_paths[run_key] = {"/app/schema.json"}
    guard._user_text[run_key] = "Write a JSON file with exact field names."

    guard.after_tool(_after("printf '{\"ok\": true}' > /app/schema.json"))
    guard.after_tool(_after("jq . /app/schema.json"))
    assert guard._scoped_final_recovery(run_key) == ""

    guard.after_tool(_after("printf '{\"ok\": false}' > /app/schema.json"))
    assert "independently written client/parser" in guard._scoped_final_recovery(run_key)


def test_masked_parser_failure_does_not_count_as_evidence() -> None:
    guard = ExecutionGuardMiddleware()
    run_key = "run_id:run"
    guard._required_paths[run_key] = {"/app/schema.json"}
    guard._user_text[run_key] = "Write a JSON file with exact field names."

    guard.after_tool(_after("printf '{bad json}' > /app/schema.json"))
    guard.after_tool(_after("jq . /app/schema.json || true"))

    assert "independently written client/parser" in guard._scoped_final_recovery(run_key)


def test_generic_numeric_threshold_does_not_force_a_final_turn() -> None:
    guard = ExecutionGuardMiddleware()
    run_key = "run_id:run"
    guard._required_paths[run_key] = {"/app/program.c"}
    guard._user_text[run_key] = "Keep /app/program.c less than 5000 bytes."

    result = guard.after_tool(_after("printf 'int main(){}' > /app/program.c"))

    assert guard._scoped_final_recovery(run_key) == ""
    assert "quantitative deliverable" in str((result.tool_output or {}).get("content", ""))


def test_domain_specific_high_confidence_gate_remains() -> None:
    guard = ExecutionGuardMiddleware()
    guard._user_text["run_id:run"] = "Fit G and 2D Raman peaks from this spectrum."

    recovery = guard._scoped_final_recovery("run_id:run")

    assert "shift=1e7/raw_x" in recovery
    assert "1580 and 2670" in recovery
