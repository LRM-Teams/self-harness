from __future__ import annotations

import argparse
import contextlib
import json
import math
import os
import sys
from dataclasses import replace
from pathlib import Path


def _load(repo_path: Path):
    sys.path.insert(0, str(repo_path.resolve()))
    from evaluation import Evaluator, get_data

    return Evaluator, get_data


def _error_type(feedback: str) -> str | None:
    lowered = feedback.lower()
    if "timeout" in lowered:
        return "timeout"
    if "exception" in lowered or "caught error" in lowered:
        return "exception"
    if "no result" in lowered:
        return "no_result"
    return None


def _error_line_count(feedback: str) -> int:
    """Count evaluator feedback lines that make a candidate infeasible."""
    return sum(_error_type(line) is not None for line in feedback.splitlines())


def _development_payload(dev_score: float, dev_feedback: str) -> dict:
    error_lines = _error_line_count(dev_feedback)
    finite_score = math.isfinite(dev_score)
    return {
        "score": dev_score if finite_score else 0.0,
        "feasible": finite_score and error_lines == 0,
        "feedback": dev_feedback,
        "metrics": {"dev_score": dev_score, "error_cases": float(error_lines)},
        "error_type": _error_type(dev_feedback),
    }


def _development_only(data):
    """Return a Data view that loads only public development instances."""
    if not all(hasattr(data, name) for name in ("get_dev", "load_data", "test_cases")):
        return data
    dev = data.get_dev()
    if dev is None:
        return data
    original_load = data.load_data
    selected_cases = [case for case in data.test_cases if case in dev]

    def load_development(file_path):
        instances = original_load(file_path)
        indices = dev.get(os.path.basename(file_path), [])
        if not indices:
            indices = [0]
        return [instances[index] for index in indices if index < len(instances)]

    return replace(
        data,
        test_cases=selected_cases,
        load_data=load_development,
        get_dev=lambda: None,
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", required=True, type=Path)
    parser.add_argument("--data", required=True, type=Path)
    parser.add_argument("--task", required=True)
    parser.add_argument("--mode", choices=("describe", "dev", "final"), required=True)
    parser.add_argument("--candidate", type=Path)
    parser.add_argument("--timeout", type=float, default=10)
    parser.add_argument("--cpu-num", type=int, default=1)
    parser.add_argument("--feedback-length", type=int, default=64)
    args = parser.parse_args()

    Evaluator, get_data = _load(args.repo)
    with contextlib.redirect_stdout(sys.stderr):
        data = get_data(args.task, src_dir=str(args.data.resolve()))
    if args.mode == "describe":
        print(json.dumps({"problem_description": data.problem_description}, ensure_ascii=False))
        return
    if args.candidate is None:
        raise ValueError("--candidate is required for evaluation")
    if args.mode == "dev":
        data = _development_only(data)
    code = args.candidate.read_text(encoding="utf-8")
    with contextlib.redirect_stdout(sys.stderr):
        evaluator = Evaluator(
            data,
            timeout=args.timeout,
            cpu_num=args.cpu_num,
            feedback_length=args.feedback_length,
        )
        feedback = evaluator.evaluate(code)

    if args.mode == "final":
        payload = {
            "test_score": float(feedback.test_score),
            "test_feedback": str(feedback.test_feedback),
        }
    else:
        dev_feedback = str(feedback.dev_feedback)
        dev_score = float(feedback.dev_score)
        payload = _development_payload(dev_score, dev_feedback)
    print(json.dumps(payload, ensure_ascii=False))


if __name__ == "__main__":
    main()
