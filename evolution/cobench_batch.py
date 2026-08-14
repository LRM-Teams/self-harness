from __future__ import annotations

import argparse
import concurrent.futures
import json
import re
import subprocess
import sys
import time
from copy import deepcopy
from pathlib import Path
from typing import Any

import yaml


OFFICIAL_TASKS = (
    "Aircraft landing",
    "Assignment problem",
    "Assortment problem",
    "Bin packing - one-dimensional",
    "Capacitated warehouse location",
    "Common due date scheduling",
    "Constrained guillotine cutting",
    "Constrained non-guillotine cutting",
    "Container loading",
    "Container loading with weight restrictions",
    "Corporate structuring",
    "Crew scheduling",
    "Equitable partitioning problem",
    "Euclidean Steiner problem",
    "Flow shop scheduling",
    "Generalised assignment problem",
    "Graph colouring",
    "Hybrid Reentrant Shop Scheduling",
    "Job shop scheduling",
    "Maximal independent set",
    "Multi-Demand Multidimensional Knapsack problem",
    "Multidimensional knapsack problem",
    "Open shop scheduling",
    "Packing unequal circles",
    "Packing unequal circles area",
    "Packing unequal rectangles and squares",
    "Packing unequal rectangles and squares area",
    "Resource constrained shortest path",
    "Set covering",
    "Set partitioning",
    "Travelling salesman problem",
    "Uncapacitated warehouse location",
    "Unconstrained guillotine cutting",
    "Vehicle routing: period routing",
    "p-median - capacitated",
    "p-median - uncapacitated",
)


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")


def _atomic_json(path: Path, payload: Any) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True),
        encoding="utf-8",
    )
    temporary.replace(path)


def _task_config(
    base: dict[str, Any], output_root: Path, index: int, task: str
) -> tuple[Path, Path]:
    task_key = f"{index + 1:02d}-{_slug(task)}"
    task_output = output_root / "tasks" / task_key
    config_path = output_root / "configs" / f"{task_key}.yaml"
    payload = deepcopy(base)
    payload["output_dir"] = str(task_output)
    payload["evaluator"]["kwargs"]["task"] = task
    config_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = config_path.with_suffix(".yaml.tmp")
    temporary.write_text(
        yaml.safe_dump(payload, sort_keys=False, allow_unicode=True), encoding="utf-8"
    )
    temporary.replace(config_path)
    return config_path, task_output


def _task_result(task: str, output_dir: Path, returncode: int, elapsed: float) -> dict[str, Any]:
    records: list[dict[str, Any]] = []
    archive_path = output_dir / "archive.json"
    if archive_path.is_file():
        records = json.loads(archive_path.read_text(encoding="utf-8")).get("candidates", [])
    evaluated = [item for item in records if item.get("evaluation_index") is not None]
    feasible = [item for item in evaluated if item.get("feasible") and item.get("score") is not None]
    selection_path = output_dir / "final" / "selection.json"
    final_path = output_dir / "final" / "final_test_result.json"
    return {
        "task": task,
        "status": "complete" if returncode == 0 else "failed",
        "returncode": returncode,
        "elapsed_seconds": round(elapsed, 3),
        "evaluations": len(evaluated),
        "development_best": max((float(item["score"]) for item in feasible), default=None),
        "selection": (
            json.loads(selection_path.read_text(encoding="utf-8"))
            if selection_path.is_file()
            else None
        ),
        "final_test": (
            json.loads(final_path.read_text(encoding="utf-8"))
            if final_path.is_file()
            else None
        ),
        "output_dir": str(output_dir),
    }


def _run_task(
    index: int,
    total: int,
    task: str,
    config_path: Path,
    task_output: Path,
    output_root: Path,
) -> tuple[str, dict[str, Any]]:
    log_dir = output_root / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_key = f"{index + 1:02d}-{_slug(task)}"
    print(f"[{index + 1}/{total}] start: {task}", flush=True)
    started = time.monotonic()
    with (log_dir / f"{log_key}.stdout.log").open("a", encoding="utf-8") as out, (
        log_dir / f"{log_key}.stderr.log"
    ).open("a", encoding="utf-8") as err:
        completed = subprocess.run(
            [sys.executable, "-m", "evolution.cobench", "--config", str(config_path)],
            stdout=out,
            stderr=err,
            check=False,
        )
    return task, _task_result(
        task, task_output, completed.returncode, time.monotonic() - started
    )


def run_batch(
    base_config: Path,
    output_root: Path,
    tasks: list[str],
    workers: int = 1,
) -> dict[str, Any]:
    if workers < 1:
        raise ValueError("workers must be at least 1")
    payload = yaml.safe_load(base_config.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("base config must be a YAML mapping")
    output_root.mkdir(parents=True, exist_ok=True)
    state_path = output_root / "summary.json"
    state: dict[str, Any] = {"schema_version": 1, "tasks": {}}
    if state_path.is_file():
        state = json.loads(state_path.read_text(encoding="utf-8"))

    pending: list[tuple[int, str, Path, Path]] = []
    for index, task in enumerate(tasks):
        existing = state.get("tasks", {}).get(task, {})
        if existing.get("status") == "complete":
            print(f"[{index + 1}/{len(tasks)}] skip complete: {task}", flush=True)
            continue
        config_path, task_output = _task_config(payload, output_root, index, task)
        pending.append((index, task, config_path, task_output))

    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(
                _run_task,
                index,
                len(tasks),
                task,
                config_path,
                task_output,
                output_root,
            ): (index, task)
            for index, task, config_path, task_output in pending
        }
        for future in concurrent.futures.as_completed(futures):
            index, expected_task = futures[future]
            try:
                task, result = future.result()
            except Exception as exc:
                task = expected_task
                result = {
                    "task": task,
                    "status": "failed",
                    "returncode": None,
                    "error": f"batch worker error: {type(exc).__name__}: {exc}",
                }
            state.setdefault("tasks", {})[task] = result
            _atomic_json(state_path, state)
            print(
                f"[{index + 1}/{len(tasks)}] {result['status']}: {task}; "
                f"evals={result.get('evaluations')} "
                f"dev={result.get('development_best')} "
                f"test={(result.get('final_test') or {}).get('test_score')}",
                flush=True,
            )
    return state


def main() -> None:
    parser = argparse.ArgumentParser(description="Run resumable Pi evolution on CO-Bench tasks")
    parser.add_argument("--base-config", required=True, type=Path)
    parser.add_argument("--output-root", required=True, type=Path)
    parser.add_argument("--task", action="append", choices=OFFICIAL_TASKS)
    parser.add_argument(
        "--workers",
        type=int,
        default=1,
        help="number of CO-Bench tasks to run concurrently (default: 1)",
    )
    args = parser.parse_args()
    tasks = list(args.task or OFFICIAL_TASKS)
    result = run_batch(args.base_config, args.output_root, tasks, workers=args.workers)
    print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
