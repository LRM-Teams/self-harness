from __future__ import annotations

import json
import os
import shlex
import subprocess
import time
from pathlib import Path
from typing import Any

from ..models import EvaluationResult, ValidationResult


class ShellEvaluationAdapter:
    """Generic adapter for evaluators that print one JSON object to stdout."""

    def __init__(
        self,
        *,
        evaluate_command: str | list[str],
        validate_command: str | list[str] | None = None,
        timeout_seconds: float = 600,
        environment: dict[str, str] | None = None,
    ):
        self.evaluate_command = evaluate_command
        self.validate_command = validate_command
        self.timeout_seconds = timeout_seconds
        self.environment = environment or {}

    def validate(self, candidate_dir: Path) -> ValidationResult:
        if self.validate_command is None:
            return ValidationResult(True)
        payload = self._run(self.validate_command, candidate_dir)
        return ValidationResult(
            valid=bool(payload.get("valid", False)),
            feedback=str(payload.get("feedback", "")),
            metrics=self._float_metrics(payload.get("metrics", {})),
        )

    def evaluate(self, candidate_dir: Path) -> EvaluationResult:
        started = time.monotonic()
        payload = self._run(self.evaluate_command, candidate_dir)
        return EvaluationResult(
            score=float(payload["score"]),
            feasible=bool(payload.get("feasible", True)),
            feedback=str(payload.get("feedback", "")),
            runtime_seconds=float(payload.get("runtime_seconds", time.monotonic() - started)),
            metrics=self._float_metrics(payload.get("metrics", {})),
            error_type=str(payload["error_type"]) if payload.get("error_type") else None,
        )

    def _run(self, command: str | list[str], candidate_dir: Path) -> dict[str, Any]:
        argv = shlex.split(command) if isinstance(command, str) else list(command)
        env = os.environ.copy()
        env.update(self.environment)
        env["CANDIDATE_DIR"] = str(candidate_dir.resolve())
        completed = subprocess.run(
            argv,
            cwd=candidate_dir,
            env=env,
            text=True,
            capture_output=True,
            timeout=self.timeout_seconds,
            check=False,
        )
        if completed.returncode != 0:
            raise RuntimeError(
                f"command exited {completed.returncode}: {(completed.stderr or completed.stdout).strip()}"
            )
        try:
            payload = json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            raise ValueError("evaluator stdout must be one JSON object") from exc
        if not isinstance(payload, dict):
            raise ValueError("evaluator JSON must be an object")
        return payload

    @staticmethod
    def _float_metrics(value: Any) -> dict[str, float]:
        if not isinstance(value, dict):
            return {}
        return {str(key): float(item) for key, item in value.items()}
