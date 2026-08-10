import json
import subprocess
from pathlib import Path

import pytest
import evolve as evolve_module

from evolve import (
    FEEDBACK_MODE_REWARD_ONLY,
    FEEDBACK_MODE_VERIFIER_OUTPUT,
    TaskAnalysisJob,
    _build_adb_jobs,
    _build_verifier_context,
    _feedback_mode,
    _read_safe_process_metadata,
    _read_verifier_output,
    _safe_task_id,
    _stage_reward_only_jobs,
    _run_single_adb_ask,
    _write_debugger_analyse,
    build_evolution_query,
    init_workspace,
)


SENTINEL = "HIDDEN_EXPECTED_VALUE_SENTINEL"
FORBIDDEN_SENTINELS = {
    "expected": "EXPECTED_VALUE_SENTINEL",
    "reference": "REFERENCE_ANSWER_SENTINEL",
    "test_name": "HIDDEN_TEST_NAME_SENTINEL",
    "test_path": "HIDDEN_TEST_PATH_SENTINEL",
    "message": "VERIFIER_MESSAGE_SENTINEL",
    "trace": "VERIFIER_TRACE_SENTINEL",
    "payload": "VERIFIER_PAYLOAD_SENTINEL",
    "stdout": "TEST_STDOUT_SENTINEL",
}
AGENT_TRACE_SENTINEL = "AGENT_OWN_TRACE_ALLOWED_SENTINEL"


def _trial(tmp_path: Path) -> tuple[Path, Path]:
    job_dir = tmp_path / "job"
    trial = job_dir / "example-task__ABC123"
    agent = trial / "agent"
    verifier = trial / "verifier"
    agent.mkdir(parents=True)
    verifier.mkdir()
    (agent / "nexau_in_memory_tracer.cleaned.json").write_text(
        json.dumps({"messages": [{"role": "assistant", "content": AGENT_TRACE_SENTINEL}]}),
        encoding="utf-8",
    )
    (verifier / "reward.txt").write_text("0", encoding="utf-8")
    (verifier / "test-stdout.txt").write_text(FORBIDDEN_SENTINELS["stdout"], encoding="utf-8")
    (trial / "result.json").write_text(json.dumps({
        "finished_at": "2026-08-07T01:00:00Z",
        "exception_info": None,
        "agent_execution": {"finished_at": "2026-08-07T00:59:00Z"},
        "config": {
            "expected_value": FORBIDDEN_SENTINELS["expected"],
            "reference_answer": FORBIDDEN_SENTINELS["reference"],
        },
        "verifier_result": {
            "message": FORBIDDEN_SENTINELS["message"],
            "trace": FORBIDDEN_SENTINELS["trace"],
            "payload": FORBIDDEN_SENTINELS["payload"],
        },
    }), encoding="utf-8")
    (verifier / "ctrf.json").write_text(json.dumps({
        "results": {
            "summary": {
                "tests": 2, "passed": 0, "failed": 1, "skipped": 1,
                "pending": 0, "other": 0, "start": 10.0, "stop": 12.5,
                "expected": FORBIDDEN_SENTINELS["expected"],
            },
            "tests": [{
                "name": FORBIDDEN_SENTINELS["test_name"],
                "file_path": FORBIDDEN_SENTINELS["test_path"],
                "status": "failed",
                "message": FORBIDDEN_SENTINELS["message"],
                "trace": FORBIDDEN_SENTINELS["trace"],
                "duration": 2.5,
            }],
        }
    }), encoding="utf-8")
    return job_dir, trial


def test_reward_only_is_default_and_never_collects_verifier_output(tmp_path: Path) -> None:
    job_dir, trial = _trial(tmp_path)
    config = {"max_tasks": 10}

    assert _feedback_mode(config) == FEEDBACK_MODE_REWARD_ONLY
    assert _read_verifier_output(trial, config) == ""

    jobs = _build_adb_jobs({"example-task": "fail"}, job_dir, config)
    assert len(jobs) == 1
    assert jobs[0].trace_rewards == [0.0]
    assert jobs[0].verifier_outputs == [""]
    context = _build_verifier_context(jobs[0])
    assert '"tests_failed":1' in context
    assert '"tests_duration_seconds":2.5' in context
    assert '"trial_finished":true' in context
    for sentinel in FORBIDDEN_SENTINELS.values():
        assert sentinel not in context
        assert sentinel not in repr(jobs[0])


def test_safe_metadata_uses_field_allowlist(tmp_path: Path) -> None:
    _, trial = _trial(tmp_path)

    metadata = _read_safe_process_metadata(trial)

    for sentinel in FORBIDDEN_SENTINELS.values():
        assert sentinel not in metadata
    assert "reference_answer" not in metadata
    assert "message" not in metadata
    assert "trace" not in metadata
    assert "file_path" not in metadata
    assert json.loads(metadata) == {
        "agent_execution_finished": True,
        "execution_exception": False,
        "tests_duration_seconds": 2.5,
        "tests_failed": 1,
        "tests_other": 0,
        "tests_passed": 0,
        "tests_pending": 0,
        "tests_skipped": 1,
        "tests_total": 2,
        "trial_finished": True,
    }


def test_verifier_output_requires_explicit_opt_in(tmp_path: Path) -> None:
    job_dir, trial = _trial(tmp_path)
    config = {"max_tasks": 10, "feedback_mode": FEEDBACK_MODE_VERIFIER_OUTPUT}

    assert _read_verifier_output(trial, config) == FORBIDDEN_SENTINELS["stdout"]
    jobs = _build_adb_jobs({"example-task": "fail"}, job_dir, config)
    context = _build_verifier_context(jobs[0])
    assert FORBIDDEN_SENTINELS["stdout"] in context


def test_invalid_feedback_mode_is_rejected() -> None:
    with pytest.raises(ValueError, match="feedback_mode"):
        _feedback_mode({"feedback_mode": "sometimes"})


def test_empty_reward_only_job_cannot_emit_verifier_context() -> None:
    job = TaskAnalysisJob(task_name="example-task", trace_rewards=[0.0], verifier_outputs=[""])
    assert _build_verifier_context(job) == ""


def test_sanitized_bundle_and_analysis_preserve_only_agent_owned_trace(tmp_path: Path) -> None:
    job_dir, trial = _trial(tmp_path)
    iteration_dir = tmp_path / "experiment" / "runs" / "iteration_001"
    config = {"max_tasks": 10, "feedback_mode": FEEDBACK_MODE_REWARD_ONLY}
    jobs = _build_adb_jobs({"example-task": "fail"}, job_dir, config)

    bundle_dir = _stage_reward_only_jobs(jobs, iteration_dir)
    job = jobs[0]
    assert job.trial_dirs == []
    assert all(bundle_dir in path.parents for path in job.trace_paths)

    results = [{
        "task_name": job.task_name,
        "safe_id": job.safe_id,
        "mode": job.mode,
        "n_pass": job.n_pass,
        "n_fail": job.n_fail,
        "n_timeout": job.n_timeout,
        "is_timeout": job.is_timeout,
        "response": "Safe analysis based only on the agent trajectory.",
        "trace_paths": [str(path) for path in job.trace_paths],
        "trace_rewards": list(job.trace_rewards),
        "verifier_outputs": list(job.verifier_outputs),
    }]
    analysis_dir, overview = _write_debugger_analyse(results, iteration_dir, 1)

    visible_text = "\n".join(
        path.read_text(encoding="utf-8")
        for root in (bundle_dir, analysis_dir)
        for path in root.rglob("*")
        if path.is_file()
    )
    assert AGENT_TRACE_SENTINEL in visible_text
    assert str(trial) not in visible_text
    for sentinel in FORBIDDEN_SENTINELS.values():
        assert sentinel not in visible_text

    stats = {
        "k": 1,
        "pass_rate": 0.0,
        "n_pass": 0,
        "n_fail": 1,
        "n_exception": 0,
        "n_total": 1,
        "exception_types": {FORBIDDEN_SENTINELS["payload"]: 1},
        "timeout_tasks": set(),
        "task_results": {"example-task": "fail"},
        "per_task_rollouts": {},
    }
    query = build_evolution_query(
        iteration=1,
        stats=stats,
        job_dir=job_dir,
        iteration_dir=iteration_dir,
        prev_stats=None,
        diff=None,
        stability=None,
        best_ever=None,
        scores_trend=None,
        adb_overview=overview,
        feedback_mode=FEEDBACK_MODE_REWARD_ONLY,
    )
    assert str(job_dir) not in query
    assert "sanitized_feedback" in query
    for sentinel in FORBIDDEN_SENTINELS.values():
        assert sentinel not in query


def test_safe_task_id_cannot_escape_detail_directory() -> None:
    safe_id = _safe_task_id("../../escape/../../../hidden")
    assert "/" not in safe_id
    assert ".." not in safe_id


def test_debugger_subprocess_receives_only_staged_trace_and_safe_query(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    job_dir, trial = _trial(tmp_path)
    iteration_dir = tmp_path / "experiment" / "runs" / "iteration_001"
    config = {"max_tasks": 10, "feedback_mode": FEEDBACK_MODE_REWARD_ONLY}
    job = _build_adb_jobs({"example-task": "fail"}, job_dir, config)[0]
    _stage_reward_only_jobs([job], iteration_dir)
    captured: dict[str, object] = {}

    def fake_invoke(cmd: list[str], env: dict[str, str], timeout: float) -> str:
        captured.update(cmd=cmd, env=env, timeout=timeout)
        return "Safe debugger response"

    monkeypatch.setattr(evolve_module, "_invoke_adb_ask_once", fake_invoke)
    result = _run_single_adb_ask(job, config, k=1)

    command_text = "\n".join(captured["cmd"])
    assert str(trial) not in command_text
    assert "sanitized_feedback" in command_text
    assert result["response"] == "Safe debugger response"
    for sentinel in FORBIDDEN_SENTINELS.values():
        assert sentinel not in command_text
    env = captured["env"]
    assert env["AHE_REWARD_ONLY_POLICY"] == "1"
    assert Path(env["ADB_RUNTIME_DIR"]).is_relative_to(iteration_dir)


def test_fresh_workspace_initialization_sets_local_git_identity(tmp_path: Path) -> None:
    source = tmp_path / "source"
    workspace = tmp_path / "workspace"
    source.mkdir()
    (source / "code_agent.yaml").write_text("type: agent\n", encoding="utf-8")

    assert init_workspace(source, workspace) is True
    assert subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=workspace, capture_output=True, check=False
    ).returncode == 0
    assert subprocess.run(
        ["git", "config", "--get", "user.name"], cwd=workspace,
        capture_output=True, text=True, check=True,
    ).stdout.strip() == "AHE Runner"
