from __future__ import annotations

import concurrent.futures
import math
import time
from dataclasses import dataclass

from .archive import CandidateArchive
from .interfaces import (
    CandidateDebugger,
    CandidateProducer,
    EvaluationAdapter,
    ExperienceCommunicator,
    ProductionContext,
)
from .models import CandidateRecord, EvaluationResult, LanePlan, ProductionResult, ValidationResult
from .scheduler import ProgressiveScheduler


@dataclass(frozen=True)
class EngineConfig:
    evaluation_budget: int = 64
    generation_workers: int = 3
    evaluation_workers: int = 1
    evaluate_locally_invalid: bool = True
    early_stop_target_score: float | None = None
    early_stop_patience_generations: int = 0


class EvolutionEngine:
    """Run independent branches, evaluate them, debug them, then exchange experience."""

    def __init__(
        self,
        *,
        archive: CandidateArchive,
        scheduler: ProgressiveScheduler,
        producer: CandidateProducer,
        evaluator: EvaluationAdapter,
        debugger: CandidateDebugger,
        communicator: ExperienceCommunicator,
        problem_path,
        config: EngineConfig,
    ):
        self.archive = archive
        self.scheduler = scheduler
        self.producer = producer
        self.evaluator = evaluator
        self.debugger = debugger
        self.communicator = communicator
        self.problem_path = problem_path.resolve()
        self.config = config

    def run(self) -> CandidateRecord | None:
        while self.archive.evaluation_count < self.config.evaluation_budget:
            if self._early_stop_reached():
                break
            remaining = self.config.evaluation_budget - self.archive.evaluation_count
            generation = self.archive.next_generation
            plans = self.scheduler.plan_generation(self.archive, remaining)
            records = [self.archive.reserve(generation, plan) for plan in plans]
            self._produce(records, plans)
            self._evaluate(records)
            if self._early_stop_reached():
                break
            self._debug(records)
            if self.scheduler.should_exchange(self.archive):
                completed = [self.archive.get(item.candidate_id) for item in records]
                memory = self.communicator.exchange(completed, self.archive.memory_path)
                self.archive.append_memory(f"## Generation {generation}\n\n{memory}")
        return self.archive.best()

    def _early_stop_reached(self) -> bool:
        target = self.config.early_stop_target_score
        if target is None:
            return False
        qualifying = [
            item
            for item in self.archive.evaluated
            if item.feasible
            and item.score is not None
            and math.isfinite(float(item.score))
            and float(item.score) >= target
        ]
        if not qualifying:
            return False
        patience = max(0, self.config.early_stop_patience_generations)
        first_generation = min(item.generation for item in qualifying)
        latest_generation = max(item.generation for item in self.archive.evaluated)
        return latest_generation >= first_generation + patience

    def _produce(self, records: list[CandidateRecord], plans: list[LanePlan]) -> None:
        def run_one(record: CandidateRecord, plan: LanePlan) -> ProductionResult:
            context = ProductionContext(
                record=record,
                plan=plan,
                artifact_dir=self.archive.artifact_dir(record.candidate_id),
                parent_dirs=tuple(self.archive.artifact_dir(item) for item in plan.parent_ids),
                reference_dirs=tuple(self.archive.artifact_dir(item) for item in plan.reference_ids),
                problem_path=self.problem_path,
                memory_path=self.archive.memory_path,
            )
            try:
                return self.producer.produce(context)
            except Exception as exc:
                return ProductionResult(summary=f"production error: {type(exc).__name__}: {exc}")

        with concurrent.futures.ThreadPoolExecutor(max_workers=self.config.generation_workers) as pool:
            futures = {
                pool.submit(run_one, record, plan): record
                for record, plan in zip(records, plans, strict=True)
            }
            for future in concurrent.futures.as_completed(futures):
                record = futures[future]
                self.archive.record_production(record.candidate_id, future.result())

    def _evaluate(self, records: list[CandidateRecord]) -> None:
        start_index = self.archive.evaluation_count

        def run_one(record: CandidateRecord) -> tuple[ValidationResult, EvaluationResult]:
            artifact_dir = self.archive.artifact_dir(record.candidate_id)
            started = time.monotonic()
            try:
                validation = self.evaluator.validate(artifact_dir)
            except Exception as exc:
                validation = ValidationResult(False, f"validator error: {type(exc).__name__}: {exc}")
            if not validation.valid and not self.config.evaluate_locally_invalid:
                result = EvaluationResult(
                    score=0.0,
                    feasible=False,
                    feedback=validation.feedback,
                    error_type="local_validation",
                )
                return validation, result
            try:
                result = self.evaluator.evaluate(artifact_dir)
            except Exception as exc:
                result = EvaluationResult(
                    score=0.0,
                    feasible=False,
                    feedback=f"evaluator error: {type(exc).__name__}: {exc}",
                    runtime_seconds=time.monotonic() - started,
                    error_type="evaluator_exception",
                )
            return validation, result

        with concurrent.futures.ThreadPoolExecutor(max_workers=self.config.evaluation_workers) as pool:
            futures = {pool.submit(run_one, record): record for record in records}
            results: dict[str, tuple[ValidationResult, EvaluationResult]] = {}
            for future in concurrent.futures.as_completed(futures):
                record = futures[future]
                results[record.candidate_id] = future.result()
        for offset, record in enumerate(records, 1):
            validation, result = results[record.candidate_id]
            self.archive.record_evaluation(record.candidate_id, validation, result, start_index + offset)

    def _debug(self, records: list[CandidateRecord]) -> None:
        def run_one(record: CandidateRecord) -> str:
            current = self.archive.get(record.candidate_id)
            try:
                return self.debugger.debug(current, self.archive.artifact_dir(record.candidate_id))
            except Exception as exc:
                return f"debug error: {type(exc).__name__}: {exc}"

        with concurrent.futures.ThreadPoolExecutor(max_workers=self.config.generation_workers) as pool:
            futures = {pool.submit(run_one, record): record for record in records}
            for future in concurrent.futures.as_completed(futures):
                record = futures[future]
                self.archive.record_debug(record.candidate_id, future.result())
