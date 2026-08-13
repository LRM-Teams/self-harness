from __future__ import annotations

from dataclasses import dataclass

from .archive import CandidateArchive
from .models import LanePlan


@dataclass(frozen=True)
class SchedulerConfig:
    branches: int = 3
    evaluation_budget: int = 64
    stagnation_window: int = 6
    invalid_window: int = 6
    invalid_rate_threshold: float = 0.5
    exchange_interval: int = 3
    diversity_weight: float = 0.35
    late_phase_fraction: float = 0.8


class ProgressiveScheduler:
    """Three-lane progressive search: elite, diverse, and adaptive exploration."""

    def __init__(self, config: SchedulerConfig):
        if config.branches < 1:
            raise ValueError("branches must be positive")
        if config.evaluation_budget < 1:
            raise ValueError("evaluation_budget must be positive")
        self.config = config

    def plan_generation(self, archive: CandidateArchive, remaining: int) -> list[LanePlan]:
        count = min(self.config.branches, remaining)
        if count <= 0:
            return []
        elite = archive.best()
        if elite is None:
            return [
                LanePlan(
                    lane=f"independent-{index + 1}",
                    operator="restart",
                    rationale="Cold-start with an independent algorithm family.",
                )
                for index in range(count)
            ]

        diverse = archive.select_diverse(elite.candidate_id, self.config.diversity_weight)
        plans = [
            LanePlan(
                lane="elite",
                operator="refine",
                parent_ids=(elite.candidate_id,),
                rationale="Exploit the highest-scoring feasible candidate.",
            )
        ]
        if count >= 2:
            parent = diverse or elite
            references = (elite.candidate_id,) if parent.candidate_id != elite.candidate_id else ()
            plans.append(
                LanePlan(
                    lane="diverse",
                    operator="refine-diverse",
                    parent_ids=(parent.candidate_id,),
                    reference_ids=references,
                    rationale="Continue a strong branch that remains structurally different from the elite.",
                )
            )
        if count >= 3:
            plans.append(self._adaptive_plan(archive, elite.candidate_id, diverse.candidate_id if diverse else None))
        return plans

    def should_exchange(self, archive: CandidateArchive) -> bool:
        if not archive.evaluated:
            return False
        generation = max(item.generation for item in archive.evaluated)
        return (
            (generation + 1) % self.config.exchange_interval == 0
            or self._stagnated(archive)
            or archive.recent_invalid_rate(self.config.invalid_window) >= self.config.invalid_rate_threshold
        )

    def _adaptive_plan(
        self,
        archive: CandidateArchive,
        elite_id: str,
        diverse_id: str | None,
    ) -> LanePlan:
        invalid_rate = archive.recent_invalid_rate(self.config.invalid_window)
        invalid = archive.latest_invalid()
        if invalid and invalid_rate >= self.config.invalid_rate_threshold:
            return LanePlan(
                lane="adaptive",
                operator="repair",
                parent_ids=(invalid.candidate_id,),
                reference_ids=(elite_id,),
                rationale="Recent invalid rate is high; repair a failed branch using the elite as a contract reference.",
            )
        progress = archive.evaluation_count / self.config.evaluation_budget
        if self._stagnated(archive):
            return LanePlan(
                lane="adaptive",
                operator="restart",
                reference_ids=tuple(item for item in (elite_id, diverse_id) if item),
                rationale="The best score has stagnated; inject a genuinely independent algorithm family.",
            )
        if diverse_id and progress < self.config.late_phase_fraction:
            return LanePlan(
                lane="adaptive",
                operator="crossover",
                parent_ids=(elite_id, diverse_id),
                rationale="Combine complementary mechanisms from high-quality, structurally distinct parents.",
            )
        return LanePlan(
            lane="adaptive",
            operator="refine",
            parent_ids=(elite_id,),
            reference_ids=(diverse_id,) if diverse_id else (),
            rationale="Late phase: spend remaining budget on focused improvement of the elite.",
        )

    def _stagnated(self, archive: CandidateArchive) -> bool:
        history = archive.best_score_history()
        window = self.config.stagnation_window
        if len(history) < window or history[-1] == float("-inf"):
            return False
        return history[-1] <= history[-window]
