from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import yaml

from evolution import cobench_batch
from evolution.cobench_batch import OFFICIAL_TASKS, _task_config, _task_result


def test_official_batch_contains_36_unique_tasks() -> None:
    assert len(OFFICIAL_TASKS) == 36
    assert len(set(OFFICIAL_TASKS)) == 36


def test_batch_writes_isolated_task_config(tmp_path: Path) -> None:
    base = {
        "output_dir": "replaced",
        "evaluator": {"kwargs": {"task": "replaced"}},
    }

    config_path, output_dir = _task_config(base, tmp_path, 0, "Assignment problem")

    rendered = config_path.read_text(encoding="utf-8")
    assert str(output_dir) in rendered
    assert "Assignment problem" in rendered
    assert base["output_dir"] == "replaced"


def test_batch_result_reads_persisted_scores(tmp_path: Path) -> None:
    output = tmp_path / "task"
    final = output / "final"
    final.mkdir(parents=True)
    (output / "archive.json").write_text(
        json.dumps(
            {
                "candidates": [
                    {"evaluation_index": 1, "feasible": False, "score": 0.2},
                    {"evaluation_index": 2, "feasible": True, "score": 0.8},
                ]
            }
        ),
        encoding="utf-8",
    )
    (final / "selection.json").write_text(
        json.dumps({"candidate_id": "c002"}), encoding="utf-8"
    )
    (final / "final_test_result.json").write_text(
        json.dumps({"test_score": 0.75}), encoding="utf-8"
    )

    result = _task_result("task", output, 0, 12.5)

    assert result["status"] == "complete"
    assert result["evaluations"] == 2
    assert result["development_best"] == 0.8
    assert result["final_test"]["test_score"] == 0.75


def test_batch_result_marks_zero_return_without_feasible_candidate_for_retry(
    tmp_path: Path,
) -> None:
    output = tmp_path / "task"
    output.mkdir()
    (output / "archive.json").write_text(
        json.dumps(
            {
                "candidates": [
                    {"evaluation_index": 1, "feasible": False, "score": 0.0},
                    {"evaluation_index": 2, "feasible": False, "score": 0.0},
                ]
            }
        ),
        encoding="utf-8",
    )

    result = _task_result("task", output, 0, 12.5)

    assert result["status"] == "no_feasible"
    assert result["selection"] is None
    assert result["final_test"] is None


def test_batch_result_marks_missing_final_test_as_failed(tmp_path: Path) -> None:
    output = tmp_path / "task"
    final = output / "final"
    final.mkdir(parents=True)
    (output / "archive.json").write_text(
        json.dumps(
            {
                "candidates": [
                    {"evaluation_index": 1, "feasible": True, "score": 0.8},
                ]
            }
        ),
        encoding="utf-8",
    )
    (final / "selection.json").write_text(
        json.dumps({"candidate_id": "c001"}), encoding="utf-8"
    )

    result = _task_result("task", output, 0, 12.5)

    assert result["status"] == "failed"
    assert result["selection"] == {"candidate_id": "c001"}
    assert result["final_test"] is None


def test_batch_runs_tasks_with_requested_concurrency(tmp_path: Path, monkeypatch) -> None:
    base_config = tmp_path / "base.yaml"
    base_config.write_text(
        yaml.safe_dump(
            {
                "output_dir": "replaced",
                "evaluator": {"kwargs": {"task": "replaced"}},
            }
        ),
        encoding="utf-8",
    )
    lock = threading.Lock()
    active = 0
    peak = 0

    def fake_run_task(index, total, task, config_path, task_output, output_root):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        time.sleep(0.05)
        with lock:
            active -= 1
        return task, {
            "task": task,
            "status": "complete",
            "evaluations": 1,
            "development_best": 1.0,
            "final_test": {"test_score": 1.0},
        }

    monkeypatch.setattr(cobench_batch, "_run_task", fake_run_task)
    tasks = list(OFFICIAL_TASKS[:6])

    result = cobench_batch.run_batch(
        base_config, tmp_path / "output", tasks, workers=4
    )

    assert peak == 4
    assert len(result["tasks"]) == len(tasks)


def test_batch_retries_no_feasible_task(tmp_path: Path, monkeypatch) -> None:
    base_config = tmp_path / "base.yaml"
    base_config.write_text(
        yaml.safe_dump(
            {
                "output_dir": "replaced",
                "evaluator": {"kwargs": {"task": "replaced"}},
            }
        ),
        encoding="utf-8",
    )
    output_root = tmp_path / "output"
    output_root.mkdir()
    task = OFFICIAL_TASKS[0]
    (output_root / "summary.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "tasks": {task: {"task": task, "status": "no_feasible"}},
            }
        ),
        encoding="utf-8",
    )
    calls: list[str] = []

    def fake_run_task(index, total, task, config_path, task_output, output_root):
        calls.append(task)
        return task, {
            "task": task,
            "status": "complete",
            "evaluations": 1,
            "development_best": 1.0,
            "final_test": {"test_score": 1.0},
        }

    monkeypatch.setattr(cobench_batch, "_run_task", fake_run_task)

    cobench_batch.run_batch(base_config, output_root, [task])

    assert calls == [task]
