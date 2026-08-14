from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

from evolution.adapters.shell import ShellEvaluationAdapter
from evolution.archive import CandidateArchive
from evolution.engine import EngineConfig, EvolutionEngine
from evolution.interfaces import ProductionContext
from evolution.models import CandidateRecord, EvaluationResult, ProductionResult, ValidationResult
from evolution.scheduler import ProgressiveScheduler, SchedulerConfig


class FakeProducer:
    def produce(self, context: ProductionContext) -> ProductionResult:
        path = context.artifact_dir / "solution.py"
        previous = path.read_text(encoding="utf-8") if path.exists() else ""
        path.write_text(previous + f"\nVALUE = {int(context.record.candidate_id[1:])}\n", encoding="utf-8")
        return ProductionResult(
            summary=f"produced {context.record.candidate_id}",
            features=(context.plan.operator, context.plan.lane),
        )


class FakeEvaluator:
    def __init__(self):
        self.calls = 0

    def validate(self, candidate_dir: Path) -> ValidationResult:
        return ValidationResult((candidate_dir / "solution.py").is_file(), "entrypoint check")

    def evaluate(self, candidate_dir: Path) -> EvaluationResult:
        self.calls += 1
        candidate_id = candidate_dir.parent.name
        value = int(candidate_id[1:])
        feasible = value != 2
        return EvaluationResult(
            score=float(value if feasible else 0),
            feasible=feasible,
            feedback=f"score={value}",
            runtime_seconds=0.01,
            error_type=None if feasible else "constraint",
        )


class FakeDebugger:
    def debug(self, record: CandidateRecord, artifact_dir: Path) -> str:
        return f"debugged {record.candidate_id}: feasible={record.feasible}"


class FakeCommunicator:
    def __init__(self):
        self.generations: list[int] = []

    def exchange(self, records: list[CandidateRecord], memory_path: Path) -> str:
        generation = records[0].generation
        self.generations.append(generation)
        return f"shared evidence from generation {generation}"


def test_engine_runs_three_independent_lanes_and_honors_budget(tmp_path: Path) -> None:
    problem = tmp_path / "problem.md"
    problem.write_text("maximize VALUE", encoding="utf-8")
    archive = CandidateArchive(tmp_path / "run")
    evaluator = FakeEvaluator()
    communicator = FakeCommunicator()
    scheduler_config = SchedulerConfig(
        branches=3,
        evaluation_budget=7,
        exchange_interval=1,
        invalid_window=3,
        invalid_rate_threshold=1.0,
    )
    engine = EvolutionEngine(
        archive=archive,
        scheduler=ProgressiveScheduler(scheduler_config),
        producer=FakeProducer(),
        evaluator=evaluator,
        debugger=FakeDebugger(),
        communicator=communicator,
        problem_path=problem,
        config=EngineConfig(
            evaluation_budget=7,
            generation_workers=3,
            evaluation_workers=3,
        ),
    )

    best = engine.run()

    assert evaluator.calls == 7
    assert best is not None and best.candidate_id == "c007"
    assert Counter(item.generation for item in archive.evaluated) == {0: 3, 1: 3, 2: 1}
    assert {item.lane for item in archive.evaluated[:3]} == {
        "independent-1",
        "independent-2",
        "independent-3",
    }
    assert all(item.debug_report for item in archive.evaluated)
    assert communicator.generations == [0, 1, 2]
    assert "shared evidence from generation 2" in archive.memory_path.read_text(encoding="utf-8")


def test_scheduler_keeps_elite_diverse_and_adaptive_lanes(tmp_path: Path) -> None:
    archive = CandidateArchive(tmp_path / "run")
    for index, source in enumerate(("greedy alpha", "tabu beta", "greedy alpha plus"), 1):
        plan = ProgressiveScheduler(SchedulerConfig()).plan_generation(archive, 1)[0]
        record = archive.reserve(index - 1, plan)
        (archive.artifact_dir(record.candidate_id) / "solution.py").write_text(source, encoding="utf-8")
        archive.record_production(record.candidate_id, ProductionResult(features=(source.split()[0],)))
        archive.record_evaluation(
            record.candidate_id,
            ValidationResult(True),
            EvaluationResult(score=float(index), feasible=True),
            index,
        )

    plans = ProgressiveScheduler(
        SchedulerConfig(branches=3, evaluation_budget=64, stagnation_window=6)
    ).plan_generation(archive, 3)

    assert [item.lane for item in plans] == ["elite", "diverse", "adaptive"]
    assert plans[0].parent_ids == ("c003",)
    assert plans[1].parent_ids
    assert plans[2].operator in {"crossover", "refine", "restart", "repair"}


def test_scheduler_adds_a_fourth_challenger_lane(tmp_path: Path) -> None:
    archive = CandidateArchive(tmp_path / "run")
    scheduler = ProgressiveScheduler(
        SchedulerConfig(branches=4, evaluation_budget=64, invalid_rate_threshold=1.0)
    )
    seed = archive.reserve(0, scheduler.plan_generation(archive, 1)[0])
    (archive.artifact_dir(seed.candidate_id) / "solution.py").write_text(
        "exact solver", encoding="utf-8"
    )
    archive.record_production(seed.candidate_id, ProductionResult())
    archive.record_evaluation(
        seed.candidate_id,
        ValidationResult(True),
        EvaluationResult(score=1.0, feasible=True),
        1,
    )

    plans = scheduler.plan_generation(archive, 4)

    assert len(plans) == 4
    assert [item.lane for item in plans] == [
        "elite",
        "diverse",
        "adaptive",
        "challenger-1",
    ]
    assert plans[3].operator == "restart"


def test_engine_stops_after_generation_reaches_target_score(tmp_path: Path) -> None:
    problem = tmp_path / "problem.md"
    problem.write_text("maximize VALUE", encoding="utf-8")
    archive = CandidateArchive(tmp_path / "run")
    evaluator = FakeEvaluator()
    communicator = FakeCommunicator()
    scheduler = ProgressiveScheduler(
        SchedulerConfig(branches=4, evaluation_budget=64, invalid_rate_threshold=1.0)
    )
    engine = EvolutionEngine(
        archive=archive,
        scheduler=scheduler,
        producer=FakeProducer(),
        evaluator=evaluator,
        debugger=FakeDebugger(),
        communicator=communicator,
        problem_path=problem,
        config=EngineConfig(
            evaluation_budget=64,
            generation_workers=4,
            evaluation_workers=1,
            early_stop_target_score=1.0,
        ),
    )

    best = engine.run()

    assert evaluator.calls == 4
    assert len(archive.evaluated) == 4
    assert best is not None and best.score == 4.0
    assert all(not item.debug_report for item in archive.evaluated)
    assert communicator.generations == []


def test_archive_persists_candidate_graph(tmp_path: Path) -> None:
    root = tmp_path / "run"
    archive = CandidateArchive(root)
    record = archive.reserve(
        0,
        ProgressiveScheduler(SchedulerConfig()).plan_generation(archive, 1)[0],
    )
    (archive.artifact_dir(record.candidate_id) / "solution.py").write_text("x = 1\n", encoding="utf-8")
    archive.record_production(record.candidate_id, ProductionResult(summary="seed"))
    archive.record_evaluation(
        record.candidate_id,
        ValidationResult(True),
        EvaluationResult(score=1.5, feasible=True),
        1,
    )

    reloaded = CandidateArchive(root)

    assert reloaded.best() is not None
    assert reloaded.best().score == 1.5
    assert reloaded.get("c001").fingerprint
    assert json.loads((root / "archive.json").read_text(encoding="utf-8"))["schema_version"] == 1


def test_shell_adapter_uses_candidate_dir_and_reads_json(tmp_path: Path) -> None:
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    script = tmp_path / "evaluate.py"
    script.write_text(
        "import json, os\n"
        "print(json.dumps({'score': 2.5, 'feasible': True, "
        "'feedback': os.environ['CANDIDATE_DIR'], 'metrics': {'quality': 2.5}}))\n",
        encoding="utf-8",
    )
    adapter = ShellEvaluationAdapter(evaluate_command=[sys.executable, str(script)])

    result = adapter.evaluate(candidate)

    assert result.score == 2.5
    assert result.feasible is True
    assert result.feedback == str(candidate.resolve())
    assert result.metrics == {"quality": 2.5}
