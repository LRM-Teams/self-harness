from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(frozen=True)
class LanePlan:
    """One independent search lane in a generation."""

    lane: str
    operator: str
    parent_ids: tuple[str, ...] = ()
    reference_ids: tuple[str, ...] = ()
    rationale: str = ""


@dataclass(frozen=True)
class ValidationResult:
    valid: bool
    feedback: str = ""
    metrics: dict[str, float] = field(default_factory=dict)


@dataclass(frozen=True)
class EvaluationResult:
    """Normalized evaluation result. Higher score must always be better."""

    score: float
    feasible: bool
    feedback: str = ""
    runtime_seconds: float = 0.0
    metrics: dict[str, float] = field(default_factory=dict)
    error_type: str | None = None


@dataclass(frozen=True)
class ProductionResult:
    summary: str = ""
    features: tuple[str, ...] = ()


@dataclass
class CandidateRecord:
    candidate_id: str
    generation: int
    lane: str
    operator: str
    artifact_path: str
    parent_ids: list[str] = field(default_factory=list)
    reference_ids: list[str] = field(default_factory=list)
    rationale: str = ""
    status: str = "reserved"
    score: float | None = None
    feasible: bool | None = None
    feedback: str = ""
    validation_feedback: str = ""
    runtime_seconds: float = 0.0
    metrics: dict[str, float] = field(default_factory=dict)
    error_type: str | None = None
    evaluation_index: int | None = None
    summary: str = ""
    features: list[str] = field(default_factory=list)
    fingerprint: str = ""
    debug_report: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "CandidateRecord":
        return cls(**value)
