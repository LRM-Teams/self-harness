from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from ..models import EvaluationResult, ValidationResult


PROJECT_DIR = Path(__file__).resolve().parent.parent.parent
WORKER_PATH = Path(__file__).resolve().with_name("cobench_worker.py")


class COBenchEvaluationAdapter:
    """Leak-resistant adapter around the official CO-Bench evaluator."""

    def __init__(
        self,
        *,
        repo_path: str,
        data_dir: str,
        task: str,
        candidate_file: str = "solution.py",
        timeout_seconds: float = 10,
        cpu_num: int = 1,
        feedback_length: int = 64,
        execution_backend: str = "docker",
        docker_image: str = "self-harness-cobench:latest",
        docker_binary: str = "docker",
        container_memory: str = "8g",
        forbidden_imports: list[str] | None = None,
        forbidden_calls: list[str] | None = None,
    ):
        self.repo_path = Path(repo_path).resolve()
        self.data_dir = Path(data_dir).resolve()
        self.task = task
        self.candidate_file = candidate_file
        self.timeout_seconds = float(timeout_seconds)
        self.cpu_num = int(cpu_num)
        self.feedback_length = int(feedback_length)
        self.execution_backend = execution_backend
        self.docker_image = docker_image
        self.docker_binary = docker_binary
        self.container_memory = container_memory
        self.forbidden_imports = set(
            forbidden_imports
            or [
                "glob",
                "httpx",
                "importlib",
                "os",
                "pathlib",
                "requests",
                "shutil",
                "socket",
                "subprocess",
                "sys",
                "urllib",
            ]
        )
        self.forbidden_calls = set(
            forbidden_calls or ["__import__", "compile", "eval", "exec", "open"]
        )
        if execution_backend not in {"local", "docker"}:
            raise ValueError("execution_backend must be local or docker")
        if self.cpu_num != 1:
            raise ValueError("CO-Bench fairness requires cpu_num=1")

    def validate(self, candidate_dir: Path) -> ValidationResult:
        path = candidate_dir / self.candidate_file
        if not path.is_file():
            return ValidationResult(False, f"missing required candidate file: {self.candidate_file}")
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except (OSError, SyntaxError) as exc:
            return ValidationResult(False, f"invalid Python: {exc}")
        functions = [node for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))]
        solve = next((node for node in functions if node.name == "solve"), None)
        if solve is None:
            return ValidationResult(False, "candidate must define top-level solve")
        if isinstance(solve, ast.AsyncFunctionDef):
            return ValidationResult(False, "solve must be synchronous")
        imports: set[str] = set()
        calls: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.update(alias.name.split(".", 1)[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imports.add(node.module.split(".", 1)[0])
            elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                calls.add(node.func.id)
        blocked_imports = sorted(imports & self.forbidden_imports)
        blocked_calls = sorted(calls & self.forbidden_calls)
        if blocked_imports or blocked_calls:
            return ValidationResult(
                False,
                "candidate violates evaluator isolation policy: "
                f"imports={blocked_imports}, calls={blocked_calls}",
            )
        return ValidationResult(True, "syntax and top-level solve contract passed")

    def evaluate(self, candidate_dir: Path) -> EvaluationResult:
        started = time.monotonic()
        payload = self._run("dev", candidate_dir / self.candidate_file)
        return EvaluationResult(
            score=float(payload["score"]),
            feasible=bool(payload["feasible"]),
            feedback=str(payload.get("feedback", "")),
            runtime_seconds=time.monotonic() - started,
            metrics={str(key): float(value) for key, value in payload.get("metrics", {}).items()},
            error_type=str(payload["error_type"]) if payload.get("error_type") else None,
        )

    def problem_description(self) -> str:
        return str(self._run("describe", None)["problem_description"])

    def final_evaluate(self, candidate_dir: Path) -> dict[str, Any]:
        """Run once after search; test feedback is never returned by evaluate()."""
        return self._run("final", candidate_dir / self.candidate_file)

    def _run(self, mode: str, candidate: Path | None) -> dict[str, Any]:
        if mode != "describe" and (candidate is None or not candidate.is_file()):
            raise FileNotFoundError(candidate)
        with tempfile.TemporaryDirectory(prefix="ahe-cobench-") as work_dir:
            if self.execution_backend == "docker":
                command = self._docker_command(mode, candidate, Path(work_dir))
                cwd = None
                env = None
            else:
                command = self._local_command(mode, candidate)
                cwd = work_dir
                env = os.environ.copy()
                env["PYTHONPATH"] = os.pathsep.join(
                    [str(PROJECT_DIR), env.get("PYTHONPATH", "")]
                ).rstrip(os.pathsep)
            # CO-Bench gives final evaluation up to one hour.  Development
            # evaluations keep the tighter watchdog because they run once per
            # candidate, while final evaluation may cover the full hidden set.
            process_timeout = (
                3660.0
                if mode == "final"
                else max(60.0, self.timeout_seconds * 20)
            )
            completed = subprocess.run(
                command,
                cwd=cwd,
                env=env,
                text=True,
                capture_output=True,
                timeout=process_timeout,
                check=False,
            )
        if completed.returncode != 0:
            raise RuntimeError(
                f"CO-Bench worker exited {completed.returncode}: "
                f"{(completed.stderr or completed.stdout).strip()}"
            )
        try:
            payload = json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid CO-Bench worker output: {completed.stdout[-1000:]}") from exc
        if not isinstance(payload, dict):
            raise ValueError("CO-Bench worker output must be a JSON object")
        if mode == "dev" and any(key.startswith("test") for key in payload):
            raise RuntimeError("refusing test leakage from CO-Bench development evaluation")
        return payload

    def _worker_args(self, mode: str, repo: str, data: str, candidate: str | None) -> list[str]:
        args = [
            "--repo", repo,
            "--data", data,
            "--task", self.task,
            "--mode", mode,
            "--timeout", str(self.timeout_seconds),
            "--cpu-num", str(self.cpu_num),
            "--feedback-length", str(self.feedback_length),
        ]
        if candidate:
            args.extend(["--candidate", candidate])
        return args

    def _local_command(self, mode: str, candidate: Path | None) -> list[str]:
        return [
            sys.executable,
            str(WORKER_PATH),
            *self._worker_args(
                mode,
                str(self.repo_path),
                str(self.data_dir),
                str(candidate.resolve()) if candidate else None,
            ),
        ]

    def _docker_command(self, mode: str, candidate: Path | None, work_dir: Path) -> list[str]:
        command = [
            self.docker_binary,
            "run",
            "--rm",
            "--network", "none",
            "--cpus", "1",
            "--memory", self.container_memory,
            "--pids-limit", "512",
            "--read-only",
            "--tmpfs", "/tmp:rw,exec,nosuid,size=2g",
            "-v", f"{PROJECT_DIR}:/opt/self-harness:ro",
            "-v", f"{self.repo_path}:/opt/co-bench:ro",
            "-v", f"{self.data_dir}:/data:ro",
            "-w", "/tmp",
        ]
        container_candidate = None
        if candidate:
            command.extend(["-v", f"{candidate.resolve()}:/candidate/solution.py:ro"])
            container_candidate = "/candidate/solution.py"
        command.extend(
            [
                self.docker_image,
                "python",
                "/opt/self-harness/evolution/adapters/cobench_worker.py",
                *self._worker_args(mode, "/opt/co-bench", "/data", container_candidate),
            ]
        )
        return command
