from __future__ import annotations

from pathlib import Path
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from types import SimpleNamespace

import pytest

from evolution.adapters.cobench import COBenchEvaluationAdapter, _run_subprocess
from evolution.archive import CandidateArchive
from evolution.cobench import COBenchScheduler
from evolution.models import EvaluationResult, ProductionResult, ValidationResult
from evolution.scheduler import SchedulerConfig
from evolution.adapters.cobench_worker import (
    _development_only,
    _development_payload,
    _error_line_count,
)


def _fake_cobench_repo(tmp_path: Path) -> tuple[Path, Path]:
    repo = tmp_path / "CO-Bench"
    package = repo / "evaluation"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text(
        "from types import SimpleNamespace\n"
        "def get_data(task, src_dir):\n"
        "    return SimpleNamespace(problem_description='PUBLIC PROBLEM ' + task)\n"
        "class Evaluator:\n"
        "    def __init__(self, data, timeout, cpu_num, feedback_length):\n"
        "        print('upstream noise')\n"
        "    def evaluate(self, code):\n"
        "        return SimpleNamespace(dev_score=0.75, dev_feedback='dev case -> Scores: 0.750', "
        "test_score=0.99, test_feedback='HIDDEN TEST FEEDBACK')\n",
        encoding="utf-8",
    )
    data = tmp_path / "data"
    data.mkdir()
    return repo, data


def test_cobench_adapter_exposes_only_development_feedback(tmp_path: Path) -> None:
    repo, data = _fake_cobench_repo(tmp_path)
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    (candidate / "solution.py").write_text("def solve(**kwargs):\n    return {}\n", encoding="utf-8")
    adapter = COBenchEvaluationAdapter(
        repo_path=str(repo),
        data_dir=str(data),
        task="Example task",
        execution_backend="local",
    )

    validation = adapter.validate(candidate)
    result = adapter.evaluate(candidate)

    assert validation.valid is True
    assert result.score == 0.75
    assert result.feasible is True
    assert result.feedback == "dev case -> Scores: 0.750"
    assert "HIDDEN" not in result.feedback
    assert all(not key.startswith("test") for key in result.metrics)
    assert adapter.problem_description() == "PUBLIC PROBLEM Example task"


def test_cobench_final_test_is_separate_from_search(tmp_path: Path) -> None:
    repo, data = _fake_cobench_repo(tmp_path)
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    (candidate / "solution.py").write_text("def solve(**kwargs):\n    return {}\n", encoding="utf-8")
    adapter = COBenchEvaluationAdapter(
        repo_path=str(repo),
        data_dir=str(data),
        task="Example task",
        execution_backend="local",
    )

    final = adapter.final_evaluate(candidate)

    assert final == {"test_score": 0.99, "test_feedback": "HIDDEN TEST FEEDBACK"}


def test_cobench_final_test_watchdog_covers_full_official_evaluator(
    tmp_path: Path, monkeypatch
) -> None:
    repo, data = _fake_cobench_repo(tmp_path)
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    (candidate / "solution.py").write_text(
        "def solve(**kwargs):\n    return {}\n", encoding="utf-8"
    )
    adapter = COBenchEvaluationAdapter(
        repo_path=str(repo),
        data_dir=str(data),
        task="Example task",
        execution_backend="local",
    )
    observed: dict[str, float] = {}

    def fake_run(*args, **kwargs):
        observed["timeout"] = kwargs["timeout"]
        return SimpleNamespace(
            returncode=0,
            stdout='{"test_score": 1.0, "test_feedback": "ok"}',
            stderr="",
        )

    monkeypatch.setattr("evolution.adapters.cobench._run_subprocess", fake_run)

    adapter.final_evaluate(candidate)

    assert observed["timeout"] == 21660.0


def test_cobench_dev_watchdog_covers_large_public_split(
    tmp_path: Path, monkeypatch
) -> None:
    repo, data = _fake_cobench_repo(tmp_path)
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    (candidate / "solution.py").write_text(
        "def solve(**kwargs):\n    return {}\n", encoding="utf-8"
    )
    adapter = COBenchEvaluationAdapter(
        repo_path=str(repo),
        data_dir=str(data),
        task="Example task",
        execution_backend="local",
    )
    observed: dict[str, float] = {}

    def fake_run(*args, **kwargs):
        observed["timeout"] = kwargs["timeout"]
        return SimpleNamespace(
            returncode=0,
            stdout=(
                '{"score": 1.0, "feasible": true, "feedback": "ok", '
                '"metrics": {"dev_score": 1.0, "error_cases": 0}}'
            ),
            stderr="",
        )

    monkeypatch.setattr("evolution.adapters.cobench._run_subprocess", fake_run)

    adapter.evaluate(candidate)

    assert observed["timeout"] == 10860.0


@pytest.mark.skipif(os.name != "posix", reason="process-session cleanup is POSIX-specific")
def test_cobench_worker_timeout_kills_descendants(tmp_path: Path) -> None:
    child_pid_path = tmp_path / "child.pid"
    script = tmp_path / "spawn_child.py"
    script.write_text(
        "import subprocess, sys, time\n"
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])\n"
        f"open({str(child_pid_path)!r}, 'w').write(str(child.pid))\n"
        "time.sleep(30)\n",
        encoding="utf-8",
    )

    with pytest.raises(subprocess.TimeoutExpired):
        _run_subprocess(
            [sys.executable, str(script)], cwd=tmp_path, env=os.environ.copy(), timeout=0.5
        )

    child_pid = int(child_pid_path.read_text(encoding="utf-8"))
    for _ in range(20):
        status = Path(f"/proc/{child_pid}/status")
        if not status.exists() or "State:\tZ" in status.read_text(encoding="utf-8"):
            break
        time.sleep(0.05)
    else:
        pytest.fail(f"timed-out worker descendant {child_pid} is still running")


def test_dev_feedback_error_markers_make_candidate_infeasible() -> None:
    feedback = "\n".join(
        [
            "a.txt -> Exception: invalid solution",
            "b.txt -> Caught Error: worker crashed",
            "c.txt -> Timeout after 10 seconds",
            "d.txt -> No result",
            "Avg Score 12.0",
        ]
    )

    payload = _development_payload(12.0, feedback)

    assert _error_line_count(feedback) == 4
    assert payload["feasible"] is False
    assert payload["metrics"]["error_cases"] == 4.0
    assert payload["error_type"] == "timeout"


def test_cobench_validation_rejects_missing_or_async_solve(tmp_path: Path) -> None:
    repo, data = _fake_cobench_repo(tmp_path)
    adapter = COBenchEvaluationAdapter(
        repo_path=str(repo), data_dir=str(data), task="task", execution_backend="local"
    )
    candidate = tmp_path / "candidate"
    candidate.mkdir()

    assert adapter.validate(candidate).valid is False
    (candidate / "solution.py").write_text("async def solve(**kwargs):\n    return {}\n", encoding="utf-8")
    assert adapter.validate(candidate).valid is False
    (candidate / "solution.py").write_text(
        "import os\ndef solve(**kwargs):\n    return {'files': os.listdir('/data')}\n",
        encoding="utf-8",
    )
    result = adapter.validate(candidate)
    assert result.valid is False
    assert "isolation policy" in result.feedback


def test_cobench_docker_command_is_single_cpu_offline_and_read_only(tmp_path: Path) -> None:
    repo, data = _fake_cobench_repo(tmp_path)
    candidate = tmp_path / "solution.py"
    candidate.write_text("def solve(**kwargs): return {}\n", encoding="utf-8")
    adapter = COBenchEvaluationAdapter(repo_path=str(repo), data_dir=str(data), task="task")

    command = adapter._docker_command("dev", candidate, tmp_path)
    rendered = " ".join(command)

    assert "--network none" in rendered
    assert "--cpus 1" in rendered
    assert "--read-only" in command
    assert f"{candidate.resolve()}:/candidate/solution.py:ro" in command
    assert "--mode dev" in rendered


def test_cobench_scheduler_keeps_early_immigrant_lane(tmp_path: Path) -> None:
    archive = CandidateArchive(tmp_path / "run")
    scheduler = COBenchScheduler(
        SchedulerConfig(branches=3, evaluation_budget=64, invalid_rate_threshold=1.0)
    )
    for index, source in enumerate(("greedy", "tabu", "beam"), 1):
        record = archive.reserve(0, scheduler.plan_generation(archive, 1)[0])
        (archive.artifact_dir(record.candidate_id) / "solution.py").write_text(source, encoding="utf-8")
        archive.record_production(record.candidate_id, ProductionResult())
        archive.record_evaluation(
            record.candidate_id,
            ValidationResult(True),
            EvaluationResult(score=float(index), feasible=True),
            index,
        )

    plans = scheduler.plan_generation(archive, 3)

    assert [item.lane for item in plans] == ["elite", "diverse", "adaptive"]
    assert plans[2].operator == "restart"
    assert plans[2].parent_ids == ()


def test_cobench_scheduler_repairs_best_partial_candidates(tmp_path: Path) -> None:
    archive = CandidateArchive(tmp_path / "run")
    scheduler = COBenchScheduler(SchedulerConfig(branches=3, evaluation_budget=64))
    for index, (source, score) in enumerate((("slow hungarian", 0.5), ("greedy", 0.25)), 1):
        record = archive.reserve(0, scheduler.plan_generation(archive, 1)[0])
        (archive.artifact_dir(record.candidate_id) / "solution.py").write_text(source, encoding="utf-8")
        archive.record_production(record.candidate_id, ProductionResult())
        archive.record_evaluation(
            record.candidate_id,
            ValidationResult(True),
            EvaluationResult(score=score, feasible=False, error_type="timeout"),
            index,
        )

    plans = scheduler.plan_generation(archive, 3)

    assert [item.operator for item in plans] == ["repair", "repair", "restart"]
    assert plans[0].parent_ids == ("c001",)
    assert plans[1].parent_ids == ("c002",)
    assert plans[2].parent_ids == ()


def test_development_view_loads_only_declared_cases_and_indices() -> None:
    @dataclass
    class Data:
        test_cases: list[str]
        load_data: object
        get_dev: object

    source = {
        "dev.txt": ["dev-0", "dev-1"],
        "test.txt": ["hidden"],
    }
    data = Data(
        test_cases=["dev.txt", "test.txt"],
        load_data=lambda path: source[Path(path).name],
        get_dev=lambda: {"dev.txt": [1]},
    )

    view = _development_only(data)

    assert view.test_cases == ["dev.txt"]
    assert view.load_data("/data/dev.txt") == ["dev-1"]
    assert view.get_dev() is None
