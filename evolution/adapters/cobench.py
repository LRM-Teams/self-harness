from __future__ import annotations

import ast
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from ..models import EvaluationResult, ValidationResult


PROJECT_DIR = Path(__file__).resolve().parent.parent.parent
WORKER_PATH = Path(__file__).resolve().with_name("cobench_worker.py")

# The largest public split in the current CO-Bench suite (Hybrid Reentrant
# Shop Scheduling) has 675 instances. The official runner is single-CPU and
# gives each instance timeout_seconds + one second to shut down, so one hour
# is not a sufficient outer watchdog. Final evaluation currently traverses
# both public and hidden instances before filtering the result.
DEFAULT_DEV_WATCHDOG_SECONDS = 3 * 60 * 60 + 60
DEFAULT_FINAL_WATCHDOG_SECONDS = 6 * 60 * 60 + 60


def _descendant_pids(root_pid: int) -> list[int]:
    """Return Linux descendants while their parent relationships still exist."""
    if os.name != "posix" or not Path("/proc").is_dir():
        return []
    children: dict[int, list[int]] = {}
    for status_path in Path("/proc").glob("[0-9]*/status"):
        try:
            pid = int(status_path.parent.name)
            ppid_line = next(
                line for line in status_path.read_text(encoding="utf-8").splitlines()
                if line.startswith("PPid:")
            )
            parent = int(ppid_line.split()[1])
        except (OSError, StopIteration, ValueError):
            continue
        children.setdefault(parent, []).append(pid)

    descendants: list[int] = []
    pending = list(children.get(root_pid, []))
    while pending:
        pid = pending.pop()
        descendants.append(pid)
        pending.extend(children.get(pid, []))
    return descendants


def _signal_process(pid: int, sig: signal.Signals) -> None:
    try:
        os.kill(pid, sig)
    except ProcessLookupError:
        pass


def _terminate_process_tree(process: subprocess.Popen[str]) -> None:
    """Terminate the worker and children, including children in new groups."""
    descendants = _descendant_pids(process.pid)
    if os.name == "posix":
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    else:
        process.terminate()
    for pid in reversed(descendants):
        _signal_process(pid, signal.SIGTERM)

    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        if os.name == "posix":
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        else:
            process.kill()
    for pid in reversed(descendants):
        _signal_process(pid, signal.SIGKILL)


def _run_subprocess(
    command: list[str],
    *,
    cwd: str | Path | None,
    env: dict[str, str] | None,
    timeout: float,
) -> subprocess.CompletedProcess[str]:
    """Run a worker in its own session and clean its process tree on timeout."""
    process = subprocess.Popen(
        command,
        cwd=cwd,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=os.name == "posix",
    )
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        _terminate_process_tree(process)
        stdout, stderr = process.communicate()
        raise subprocess.TimeoutExpired(
            command,
            timeout,
            output=stdout or exc.output,
            stderr=stderr or exc.stderr,
        ) from exc
    return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)


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
        dev_watchdog_seconds: float = DEFAULT_DEV_WATCHDOG_SECONDS,
        final_watchdog_seconds: float = DEFAULT_FINAL_WATCHDOG_SECONDS,
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
        self.dev_watchdog_seconds = float(dev_watchdog_seconds)
        self.final_watchdog_seconds = float(final_watchdog_seconds)
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
        if self.dev_watchdog_seconds <= 0 or self.final_watchdog_seconds <= 0:
            raise ValueError("CO-Bench watchdogs must be positive")

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
            # The official timeout applies to each instance, not to the whole
            # evaluator subprocess. Keep separate outer limits because final
            # evaluation traverses public and hidden instances before filtering.
            process_timeout = {
                "dev": self.dev_watchdog_seconds,
                "final": self.final_watchdog_seconds,
            }.get(mode, max(60.0, self.timeout_seconds * 20))
            completed = _run_subprocess(
                command,
                cwd=cwd,
                env=env,
                timeout=process_timeout,
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
