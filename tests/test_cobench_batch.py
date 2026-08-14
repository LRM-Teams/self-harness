from __future__ import annotations

import json
from pathlib import Path

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
