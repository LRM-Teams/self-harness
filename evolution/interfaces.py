from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from .models import CandidateRecord, EvaluationResult, LanePlan, ProductionResult, ValidationResult


@dataclass(frozen=True)
class ProductionContext:
    record: CandidateRecord
    plan: LanePlan
    artifact_dir: Path
    parent_dirs: tuple[Path, ...]
    reference_dirs: tuple[Path, ...]
    problem_path: Path
    memory_path: Path


class CandidateProducer(Protocol):
    def produce(self, context: ProductionContext) -> ProductionResult: ...


class CandidateDebugger(Protocol):
    def debug(self, record: CandidateRecord, artifact_dir: Path) -> str: ...


class ExperienceCommunicator(Protocol):
    def exchange(self, records: list[CandidateRecord], memory_path: Path) -> str: ...


class EvaluationAdapter(Protocol):
    """Benchmark boundary. evaluate() consumes exactly one formal candidate slot."""

    def validate(self, candidate_dir: Path) -> ValidationResult: ...

    def evaluate(self, candidate_dir: Path) -> EvaluationResult: ...
