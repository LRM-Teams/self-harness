import json
from pathlib import Path

import pytest

from evolve import (
    FEEDBACK_MODE_REWARD_ONLY,
    FEEDBACK_MODE_VERIFIER_OUTPUT,
    TaskAnalysisJob,
    _build_adb_jobs,
    _build_verifier_context,
    _feedback_mode,
    _read_safe_process_metadata,
    _read_verifier_output,
)


SENTINEL = "HIDDEN_EXPECTED_VALUE_SENTINEL"


def _trial(tmp_path: Path) -> tuple[Path, Path]:
    job_dir = tmp_path / "job"
    trial = job_dir / "example-task__ABC123"
    agent = trial / "agent"
    verifier = trial / "verifier"
    agent.mkdir(parents=True)
    verifier.mkdir()
    (agent / "nexau_in_memory_tracer.cleaned.json").write_text(
        '{"messages": []}', encoding="utf-8"
    )
    (verifier / "reward.txt").write_text("0", encoding="utf-8")
    (verifier / "test-stdout.txt").write_text(SENTINEL, encoding="utf-8")
    (trial / "result.json").write_text(json.dumps({
        "finished_at": "2026-08-07T01:00:00Z",
        "exception_info": None,
        "agent_execution": {"finished_at": "2026-08-07T00:59:00Z"},
        "config": {"reference_answer": SENTINEL},
        "verifier_result": {"message": SENTINEL},
    }), encoding="utf-8")
    (verifier / "ctrf.json").write_text(json.dumps({
        "results": {
            "summary": {
                "tests": 2, "passed": 0, "failed": 1, "skipped": 1,
                "pending": 0, "other": 0, "start": 10.0, "stop": 12.5,
                "expected": SENTINEL,
            },
            "tests": [{
                "name": SENTINEL, "file_path": SENTINEL, "status": "failed",
                "message": SENTINEL, "trace": SENTINEL, "duration": 2.5,
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
    assert SENTINEL not in context
    assert SENTINEL not in repr(jobs[0])


def test_safe_metadata_uses_field_allowlist(tmp_path: Path) -> None:
    _, trial = _trial(tmp_path)

    metadata = _read_safe_process_metadata(trial)

    assert SENTINEL not in metadata
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

    assert _read_verifier_output(trial, config) == SENTINEL
    jobs = _build_adb_jobs({"example-task": "fail"}, job_dir, config)
    context = _build_verifier_context(jobs[0])
    assert SENTINEL in context


def test_invalid_feedback_mode_is_rejected() -> None:
    with pytest.raises(ValueError, match="feedback_mode"):
        _feedback_mode({"feedback_mode": "sometimes"})


def test_empty_reward_only_job_cannot_emit_verifier_context() -> None:
    job = TaskAnalysisJob(task_name="example-task", trace_rewards=[0.0], verifier_outputs=[""])
    assert _build_verifier_context(job) == ""
