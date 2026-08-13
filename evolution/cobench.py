from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path

from dotenv import load_dotenv

from .adapters.cobench import COBenchEvaluationAdapter
from .archive import CandidateArchive
from .cli import build_engine_from_config
from .config import import_symbol, load_config, resolve_path
from .models import LanePlan
from .scheduler import ProgressiveScheduler, SchedulerConfig


class COBenchScheduler(ProgressiveScheduler):
    """Keep an immigrant lane early, then progressively move to crossover/refinement."""

    def plan_generation(self, archive: CandidateArchive, remaining: int) -> list[LanePlan]:
        if archive.best() is not None or not archive.evaluated:
            return super().plan_generation(archive, remaining)
        count = min(self.config.branches, remaining)
        scored = [item for item in archive.evaluated if item.score is not None]
        if not scored:
            return super().plan_generation(archive, remaining)
        elite = max(scored, key=lambda item: (float(item.score), item.evaluation_index or 0))
        alternatives = [item for item in scored if item.candidate_id != elite.candidate_id]
        diverse = max(
            alternatives,
            key=lambda item: (
                float(item.score)
                + self.config.diversity_weight
                * (1.0 - archive.similarity(elite.candidate_id, item.candidate_id))
            ),
            default=None,
        )
        plans = [
            LanePlan(
                lane="elite-repair",
                operator="repair",
                parent_ids=(elite.candidate_id,),
                rationale="No candidate is fully feasible; repair the highest-scoring partial solution.",
            )
        ]
        if count >= 2:
            parent = diverse or elite
            plans.append(
                LanePlan(
                    lane="diverse-repair",
                    operator="repair",
                    parent_ids=(parent.candidate_id,),
                    reference_ids=(elite.candidate_id,)
                    if parent.candidate_id != elite.candidate_id
                    else (),
                    rationale="Repair a structurally distinct partial solution without collapsing into the elite.",
                )
            )
        if count >= 3:
            plans.append(
                LanePlan(
                    lane="adaptive",
                    operator="restart",
                    reference_ids=tuple(
                        item for item in (elite.candidate_id, diverse.candidate_id if diverse else None) if item
                    ),
                    rationale="Preserve one independent immigrant while the current population is infeasible.",
                )
            )
        return plans

    def _adaptive_plan(
        self,
        archive: CandidateArchive,
        elite_id: str,
        diverse_id: str | None,
    ) -> LanePlan:
        invalid = archive.latest_invalid()
        if (
            invalid
            and archive.recent_invalid_rate(self.config.invalid_window)
            >= self.config.invalid_rate_threshold
        ):
            return super()._adaptive_plan(archive, elite_id, diverse_id)
        progress = archive.evaluation_count / self.config.evaluation_budget
        if progress < 0.25 or self._stagnated(archive):
            reason = (
                "Early CO-Bench search reserves one lane for a new algorithm family."
                if progress < 0.25
                else "Development score stagnated; inject a new algorithm family."
            )
            return LanePlan(
                lane="adaptive",
                operator="restart",
                reference_ids=tuple(item for item in (elite_id, diverse_id) if item),
                rationale=reason,
            )
        return super()._adaptive_plan(archive, elite_id, diverse_id)


def _scheduler(config: dict) -> COBenchScheduler:
    evolution = config.get("evolution", {})
    return COBenchScheduler(
        SchedulerConfig(
            branches=int(evolution.get("branches", 3)),
            evaluation_budget=int(evolution.get("evaluation_budget", 64)),
            stagnation_window=int(evolution.get("stagnation_window", 6)),
            invalid_window=int(evolution.get("invalid_window", 6)),
            invalid_rate_threshold=float(evolution.get("invalid_rate_threshold", 0.34)),
            exchange_interval=int(evolution.get("exchange_interval", 3)),
            diversity_weight=float(evolution.get("diversity_weight", 0.35)),
            late_phase_fraction=float(evolution.get("late_phase_fraction", 0.8)),
        )
    )


def run(config_path: Path) -> dict:
    load_dotenv()
    config = load_config(config_path)
    adapter_config = config["evaluator"]
    adapter_type = import_symbol(str(adapter_config["import_path"]))
    adapter = adapter_type(**dict(adapter_config.get("kwargs", {})))
    if not isinstance(adapter, COBenchEvaluationAdapter):
        raise TypeError("pi-cobench-evolution requires COBenchEvaluationAdapter")

    output_dir = resolve_path(config, config["output_dir"])
    output_dir.mkdir(parents=True, exist_ok=True)
    run_metadata = {
        "schema_version": 1,
        "task": adapter.task,
        "repo_path": str(adapter.repo_path),
        "data_dir": str(adapter.data_dir),
        "timeout_seconds": adapter.timeout_seconds,
        "cpu_num": adapter.cpu_num,
        "candidate_file": adapter.candidate_file,
    }
    metadata_path = output_dir / "cobench_run.json"
    if metadata_path.is_file():
        existing = json.loads(metadata_path.read_text(encoding="utf-8"))
        if existing != run_metadata:
            raise RuntimeError(
                "CO-Bench output_dir belongs to a different task or evaluator configuration"
            )
    else:
        metadata_path.write_text(
            json.dumps(run_metadata, indent=2, ensure_ascii=False), encoding="utf-8"
        )
    problem_path = output_dir / "problem.md"
    if not problem_path.exists():
        problem_path.write_text(adapter.problem_description().rstrip() + "\n", encoding="utf-8")

    engine = build_engine_from_config(
        config,
        problem_path_override=problem_path,
        adapter_override=adapter,
        scheduler_override=_scheduler(config),
    )
    best = engine.run()
    if best is None:
        return {"best": None, "final_test": None}

    final_dir = output_dir / "final"
    final_dir.mkdir(parents=True, exist_ok=True)
    shutil.copytree(engine.archive.artifact_dir(best.candidate_id), final_dir, dirs_exist_ok=True)
    selection = {
        "candidate_id": best.candidate_id,
        "development_score": best.score,
        "feasible": best.feasible,
        "evaluation_index": best.evaluation_index,
        "parent_ids": best.parent_ids,
        "reference_ids": best.reference_ids,
    }
    (final_dir / "selection.json").write_text(
        json.dumps(selection, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    final_test = None
    if bool(config.get("cobench", {}).get("run_final_test", True)):
        final_result_path = final_dir / "final_test_result.json"
        if final_result_path.is_file():
            final_test = json.loads(final_result_path.read_text(encoding="utf-8"))
        else:
            final_test = adapter.final_evaluate(final_dir)
            final_result_path.write_text(
                json.dumps(final_test, indent=2, ensure_ascii=False), encoding="utf-8"
            )
    return {"best": selection, "final_test": final_test}


def main() -> None:
    parser = argparse.ArgumentParser(description="Run Pi + DeepSeek evolution on CO-Bench")
    parser.add_argument("--config", required=True, type=Path)
    args = parser.parse_args()
    print(json.dumps(run(args.config), indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
