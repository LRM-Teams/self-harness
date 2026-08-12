from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace

from nexau.archs.main_sub.execution.hooks import BeforeToolHookInput


ROOT = Path(__file__).resolve().parents[1]
GUARD_PATH = (
    ROOT
    / "harness_inputs"
    / "cycle6-input-20260812-032740"
    / "middleware"
    / "execution_guard.py"
)
SPEC = importlib.util.spec_from_file_location("cycle6_execution_guard", GUARD_PATH)
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


def test_soft_placeholder_words_do_not_block_required_source_code() -> None:
    guard = ExecutionGuardMiddleware()
    guard._required_paths["run_id:run"] = {"/app/steal.py"}

    result = guard.before_tool(
        _before("cat > /app/steal.py <<'PY'\ndef fake_forward(dummy_input):\n    return dummy_input\nPY")
    )

    assert result.tool_input is None


def test_soft_placeholders_still_block_final_data_artifacts() -> None:
    guard = ExecutionGuardMiddleware()
    guard._required_paths["run_id:run"] = {"/app/results.json"}

    result = guard.before_tool(_before("printf '{\"value\": \"dummy\"}' > /app/results.json"))

    assert result.tool_input is not None
    assert "blocked placeholder content" in result.tool_input["command"]


def test_strong_placeholders_still_block_required_source_code() -> None:
    guard = ExecutionGuardMiddleware()
    guard._required_paths["run_id:run"] = {"/app/solution.py"}

    result = guard.before_tool(_before("printf 'TODO' > /app/solution.py"))

    assert result.tool_input is not None
    assert "blocked placeholder content" in result.tool_input["command"]


def test_raman_gate_locks_axis_and_physical_peak_domain() -> None:
    guard = ExecutionGuardMiddleware()
    guard._user_text["run_id:run"] = "Fit the G and 2D Raman peaks from the spectrum."

    recovery = guard._scoped_final_recovery("run_id:run")

    assert "raw-axis" in recovery
    assert "shift=1e7/raw_x" in recovery
    assert "1580 and 2670" in recovery
    assert "keep that coordinate system fixed" in recovery


def test_biosequence_gate_requires_concrete_synthesizable_dna() -> None:
    guard = ExecutionGuardMiddleware()
    guard._user_text["run_id:run"] = "Design a gBlock DNA sequence from PDB protein FASTA records."

    recovery = guard._scoped_final_recovery("run_id:run")

    assert "alphabet is exactly A/C/G/T" in recovery
    assert "modified/unknown residues" in recovery
    assert "never encode an amino-acid X" in recovery


def test_tensor_parallel_gate_requires_both_distributed_contexts_and_gradients() -> None:
    guard = ExecutionGuardMiddleware()
    guard._user_text["run_id:run"] = (
        "Implement ColumnParallelLinear and RowParallelLinear using torch.distributed "
        "for world_size values 1, 2, and 4."
    )

    recovery = guard._scoped_final_recovery("run_id:run")

    assert "real initialized process group" in recovery
    assert "rank/world-size-only monkeypatched context" in recovery
    assert "input, weight, and bias gradients" in recovery
    assert "full and already-sharded row inputs" in recovery


def test_golden_gate_check_prefers_biological_boundaries() -> None:
    guard = ExecutionGuardMiddleware()
    guard._user_text["run_id:run"] = "Design BsaI-HF Golden Gate primers for these coding parts."

    recovery = guard._scoped_final_recovery("run_id:run")

    assert "Simulate Type-IIS cuts" in recovery
    assert "biological part boundaries" in recovery
    assert "coincidental short overlap" in recovery
    assert "unique non-palindromic high-fidelity overhangs" in recovery
