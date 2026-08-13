from __future__ import annotations

import argparse
import json
from pathlib import Path

from dotenv import load_dotenv

from .archive import CandidateArchive
from .config import import_symbol, load_config, resolve_path
from .engine import EngineConfig, EvolutionEngine
from .pi_agents import (
    PiCandidateDebugger,
    PiCandidateProducer,
    PiEvolutionSettings,
    PiExperienceCommunicator,
)
from .scheduler import ProgressiveScheduler, SchedulerConfig


def build_engine(config_path: Path) -> EvolutionEngine:
    load_dotenv()
    config = load_config(config_path)
    evolution = config.get("evolution", {})
    pi = config["pi"]
    adapter_config = config["evaluator"]
    output_dir = resolve_path(config, config["output_dir"])
    problem_path = resolve_path(config, config["problem_path"])
    ca_path = pi.get("ca_cert_path")
    settings = PiEvolutionSettings(
        model=str(pi["model"]),
        base_url=str(pi["base_url"]),
        api_key=str(pi["api_key"]),
        candidate_entrypoint=str(config["candidate_entrypoint"]),
        ca_cert_path=resolve_path(config, ca_path) if ca_path else None,
        timeout_seconds=float(pi.get("timeout_seconds", 900)),
        enable_search=bool(pi.get("enable_search", False)),
    )
    budget = int(evolution.get("evaluation_budget", 64))
    branches = int(evolution.get("branches", 3))
    scheduler_config = SchedulerConfig(
        branches=branches,
        evaluation_budget=budget,
        stagnation_window=int(evolution.get("stagnation_window", 6)),
        invalid_window=int(evolution.get("invalid_window", 6)),
        invalid_rate_threshold=float(evolution.get("invalid_rate_threshold", 0.5)),
        exchange_interval=int(evolution.get("exchange_interval", 3)),
        diversity_weight=float(evolution.get("diversity_weight", 0.35)),
        late_phase_fraction=float(evolution.get("late_phase_fraction", 0.8)),
    )
    adapter_type = import_symbol(str(adapter_config["import_path"]))
    adapter = adapter_type(**dict(adapter_config.get("kwargs", {})))
    archive = CandidateArchive(output_dir)
    return EvolutionEngine(
        archive=archive,
        scheduler=ProgressiveScheduler(scheduler_config),
        producer=PiCandidateProducer(output_dir, settings),
        evaluator=adapter,
        debugger=PiCandidateDebugger(output_dir, settings),
        communicator=PiExperienceCommunicator(output_dir, settings),
        problem_path=problem_path,
        config=EngineConfig(
            evaluation_budget=budget,
            generation_workers=int(evolution.get("generation_workers", branches)),
            evaluation_workers=int(evolution.get("evaluation_workers", 1)),
            evaluate_locally_invalid=bool(evolution.get("evaluate_locally_invalid", True)),
        ),
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Run evaluator-agnostic Pi candidate evolution")
    parser.add_argument("--config", required=True, type=Path)
    args = parser.parse_args()
    engine = build_engine(args.config)
    best = engine.run()
    print(json.dumps(best.to_dict() if best else None, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
