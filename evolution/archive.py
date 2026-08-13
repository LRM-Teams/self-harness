from __future__ import annotations

import hashlib
import json
import math
import re
import shutil
import threading
from pathlib import Path

from .models import CandidateRecord, EvaluationResult, LanePlan, ProductionResult, ValidationResult


_TOKEN_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*|\d+(?:\.\d+)?|[^\s]")


class CandidateArchive:
    """Durable candidate graph with parent and cross-branch reference edges."""

    def __init__(self, root: Path):
        self.root = root.resolve()
        self.candidates_dir = self.root / "candidates"
        self.sessions_dir = self.root / "sessions"
        self.memory_path = self.root / "retrospective_memory.md"
        self.state_path = self.root / "archive.json"
        self._lock = threading.RLock()
        self.candidates_dir.mkdir(parents=True, exist_ok=True)
        self.sessions_dir.mkdir(parents=True, exist_ok=True)
        if not self.memory_path.exists():
            self.memory_path.write_text("# Retrospective Memory\n\n", encoding="utf-8")
        self._records: list[CandidateRecord] = []
        if self.state_path.is_file():
            payload = json.loads(self.state_path.read_text(encoding="utf-8"))
            self._records = [CandidateRecord.from_dict(item) for item in payload.get("candidates", [])]

    @property
    def records(self) -> list[CandidateRecord]:
        with self._lock:
            return [CandidateRecord.from_dict(item.to_dict()) for item in self._records]

    @property
    def evaluated(self) -> list[CandidateRecord]:
        return [item for item in self.records if item.evaluation_index is not None]

    @property
    def evaluation_count(self) -> int:
        return len(self.evaluated)

    @property
    def next_generation(self) -> int:
        return max((item.generation for item in self.records), default=-1) + 1

    def artifact_dir(self, candidate_id: str) -> Path:
        return self.candidates_dir / candidate_id / "artifact"

    def get(self, candidate_id: str) -> CandidateRecord:
        for item in self.records:
            if item.candidate_id == candidate_id:
                return item
        raise KeyError(candidate_id)

    def reserve(self, generation: int, plan: LanePlan) -> CandidateRecord:
        with self._lock:
            candidate_id = f"c{len(self._records) + 1:03d}"
            artifact_dir = self.artifact_dir(candidate_id)
            artifact_dir.mkdir(parents=True, exist_ok=False)
            if plan.parent_ids:
                source = self.artifact_dir(plan.parent_ids[0])
                if source.is_dir():
                    shutil.copytree(source, artifact_dir, dirs_exist_ok=True)
            record = CandidateRecord(
                candidate_id=candidate_id,
                generation=generation,
                lane=plan.lane,
                operator=plan.operator,
                artifact_path=str(artifact_dir.relative_to(self.root)),
                parent_ids=list(plan.parent_ids),
                reference_ids=list(plan.reference_ids),
                rationale=plan.rationale,
            )
            self._records.append(record)
            self._save()
            return CandidateRecord.from_dict(record.to_dict())

    def record_production(self, candidate_id: str, result: ProductionResult) -> None:
        with self._lock:
            item = self._mutable(candidate_id)
            item.status = "produced"
            item.summary = result.summary
            item.features = list(result.features)
            item.fingerprint = self._fingerprint(self.artifact_dir(candidate_id))
            self._save()

    def record_evaluation(
        self,
        candidate_id: str,
        validation: ValidationResult,
        result: EvaluationResult,
        evaluation_index: int,
    ) -> None:
        with self._lock:
            item = self._mutable(candidate_id)
            item.status = "evaluated"
            item.validation_feedback = validation.feedback
            item.score = float(result.score)
            item.feasible = bool(result.feasible)
            item.feedback = result.feedback
            item.runtime_seconds = float(result.runtime_seconds)
            item.metrics = dict(result.metrics)
            item.error_type = result.error_type
            item.evaluation_index = evaluation_index
            item.fingerprint = item.fingerprint or self._fingerprint(self.artifact_dir(candidate_id))
            self._save()

    def record_debug(self, candidate_id: str, report: str) -> None:
        with self._lock:
            self._mutable(candidate_id).debug_report = report
            self._save()

    def best(self) -> CandidateRecord | None:
        feasible = [item for item in self.evaluated if item.feasible and item.score is not None]
        return max(feasible, key=lambda item: (float(item.score), -(item.evaluation_index or 0)), default=None)

    def latest_invalid(self) -> CandidateRecord | None:
        invalid = [item for item in self.evaluated if not item.feasible]
        return max(invalid, key=lambda item: item.evaluation_index or 0, default=None)

    def select_diverse(self, elite_id: str, diversity_weight: float = 0.35) -> CandidateRecord | None:
        elite = self.get(elite_id)
        pool = [item for item in self.evaluated if item.feasible and item.candidate_id != elite_id]
        if not pool:
            return None
        scores = [float(item.score or 0.0) for item in pool + [elite]]
        low, high = min(scores), max(scores)

        def objective(item: CandidateRecord) -> tuple[float, float]:
            quality = 1.0 if math.isclose(low, high) else (float(item.score or 0.0) - low) / (high - low)
            novelty = 1.0 - self.similarity(elite.candidate_id, item.candidate_id)
            return quality + diversity_weight * novelty, novelty

        return max(pool, key=objective)

    def similarity(self, left_id: str, right_id: str) -> float:
        left = self._shingles(self.artifact_dir(left_id))
        right = self._shingles(self.artifact_dir(right_id))
        if not left and not right:
            return 1.0
        union = left | right
        return len(left & right) / len(union) if union else 1.0

    def best_score_history(self) -> list[float]:
        best = float("-inf")
        history: list[float] = []
        for item in sorted(self.evaluated, key=lambda value: value.evaluation_index or 0):
            if item.feasible and item.score is not None:
                best = max(best, float(item.score))
            history.append(best)
        return history

    def recent_invalid_rate(self, window: int) -> float:
        recent = sorted(self.evaluated, key=lambda value: value.evaluation_index or 0)[-window:]
        return sum(not bool(item.feasible) for item in recent) / len(recent) if recent else 0.0

    def append_memory(self, text: str) -> None:
        cleaned = text.strip()
        if not cleaned:
            return
        with self._lock:
            with self.memory_path.open("a", encoding="utf-8") as handle:
                handle.write("\n" + cleaned + "\n")

    def _mutable(self, candidate_id: str) -> CandidateRecord:
        for item in self._records:
            if item.candidate_id == candidate_id:
                return item
        raise KeyError(candidate_id)

    def _save(self) -> None:
        payload = {"schema_version": 1, "candidates": [item.to_dict() for item in self._records]}
        temporary = self.state_path.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        temporary.replace(self.state_path)

    @staticmethod
    def _fingerprint(path: Path) -> str:
        digest = hashlib.sha256()
        for file_path in sorted(item for item in path.rglob("*") if item.is_file()):
            digest.update(str(file_path.relative_to(path)).encode())
            digest.update(file_path.read_bytes())
        return digest.hexdigest()

    @staticmethod
    def _shingles(path: Path, width: int = 5) -> set[str]:
        tokens: list[str] = []
        for file_path in sorted(item for item in path.rglob("*") if item.is_file()):
            try:
                tokens.extend(_TOKEN_RE.findall(file_path.read_text(encoding="utf-8")))
            except (OSError, UnicodeDecodeError):
                continue
        if len(tokens) < width:
            return {" ".join(tokens)} if tokens else set()
        return {" ".join(tokens[index:index + width]) for index in range(len(tokens) - width + 1)}
