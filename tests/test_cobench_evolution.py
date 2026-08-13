from __future__ import annotations

from pathlib import Path

from evolution.adapters.cobench import COBenchEvaluationAdapter
from evolution.archive import CandidateArchive
from evolution.cobench import COBenchScheduler
from evolution.models import EvaluationResult, ProductionResult, ValidationResult
from evolution.scheduler import SchedulerConfig


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
