# Copyright 2025 Google LLC
# SPDX-License-Identifier: Apache-2.0
"""
run_shell_command tool (shell) - Executes shell commands.

Based on gemini-cli's shell.ts implementation.
Supports foreground and background execution, timeout handling, and process management.
"""

import shlex
import time
from collections.abc import Callable
from typing import Any

from nexau.archs.main_sub.agent_state import AgentState
from nexau.archs.sandbox import BaseSandbox, SandboxStatus
from nexau.archs.tool.builtin._sandbox_utils import get_sandbox, resolve_path

# Configuration constants.
DEFAULT_TIMEOUT_MS = 300000  # 5 minutes default timeout

# The benchmark failures showed many later tasks never received an LLM turn because
# earlier runs kept large apt/pip/build/test logs in the conversation.  The sandbox
# already saves full stdout/stderr to files, so return compact summaries by default
# and let the agent explicitly inspect the log path when details matter.
GENERAL_TRUNCATE_THRESHOLD = 4_000
GENERAL_HEAD_CHARS = 1_700
GENERAL_TAIL_CHARS = 1_900
INSPECTION_TRUNCATE_THRESHOLD = 6_000
INSPECTION_HEAD_CHARS = 2_700
INSPECTION_TAIL_CHARS = 2_700
VERBOSE_SUCCESS_THRESHOLD = 2_500
VERBOSE_SUCCESS_HEAD_CHARS = 600
VERBOSE_SUCCESS_TAIL_CHARS = 1_300
MAX_TRUNCATED_LINE_WIDTH = 1_000

NOISY_SUCCESS_COMMAND_MARKERS = (
    "apt-get ",
    "apt ",
    "pip install",
    "python -m pip install",
    "uv pip install",
    "npm install",
    "pnpm install",
    "yarn install",
    "cargo build",
    "cargo test",
    "go test",
    "go build",
    "cmake ",
    "make ",
    "ninja ",
    "pytest",
    "rspec",
    "mvn ",
    "gradle ",
    "git clone",
    "wget ",
    "curl ",
)


def _is_noisy_success_command(command: str) -> bool:
    normalized = f" {command.lower()} "
    return any(marker in normalized for marker in NOISY_SUCCESS_COMMAND_MARKERS)


def _is_file_inspection_command(command: str) -> bool:
    """Heuristic for commands where a larger snippet is useful for code reading."""
    normalized = f" {command.lower().strip()} "
    if "cat >" in normalized or "<<'eof'" in normalized or '<<"eof"' in normalized:
        return False
    markers = (
        " sed -n ",
        " grep ",
        " rg ",
        " head ",
        " tail ",
        " find ",
        " ls ",
        " awk ",
        " xxd ",
        " objdump ",
        " readelf ",
    )
    return any(marker in normalized for marker in markers)


def _clip_long_lines(text: str) -> str:
    processed: list[str] = []
    for line in text.split("\n"):
        if len(line) > MAX_TRUNCATED_LINE_WIDTH:
            processed.append(line[:MAX_TRUNCATED_LINE_WIDTH] + "... [LINE WIDTH TRUNCATED]")
        else:
            processed.append(line)
    return "\n".join(processed)


def _output_paths_note(stdout_file: str | None, stderr_file: str | None) -> str:
    paths: list[str] = []
    if stdout_file:
        paths.append(f"stdout={stdout_file}")
    if stderr_file:
        paths.append(f"stderr={stderr_file}")
    if not paths:
        return ""
    return " Full output saved in " + ", ".join(paths) + "."


def _head_tail(content: str, head_chars: int, tail_chars: int) -> str:
    if len(content) <= head_chars + tail_chars:
        return _clip_long_lines(content)
    head = _clip_long_lines(content[:head_chars].rstrip())
    tail = _clip_long_lines(content[-tail_chars:].lstrip())
    omitted = len(content) - head_chars - tail_chars
    return f"{head}\n\n... [{omitted:,} chars omitted] ...\n\n{tail}"


def _format_shell_output(
    content: str,
    *,
    command: str,
    exit_code: int,
    stdout_file: str | None,
    stderr_file: str | None,
) -> tuple[str, bool, int]:
    """Return an LLM-friendly shell output plus truncation metadata."""
    original_length = len(content)
    if original_length == 0:
        return content, False, original_length

    paths_note = _output_paths_note(stdout_file, stderr_file)

    if (
        exit_code == 0
        and original_length > VERBOSE_SUCCESS_THRESHOLD
        and _is_noisy_success_command(command)
    ):
        summary = _head_tail(
            content,
            VERBOSE_SUCCESS_HEAD_CHARS,
            VERBOSE_SUCCESS_TAIL_CHARS,
        )
        return (
            "Command succeeded. Verbose install/build/test output was compacted "
            "to save LLM context; inspect the saved log if details are needed."
            f"{paths_note}\n{summary}",
            True,
            original_length,
        )

    if _is_file_inspection_command(command):
        threshold = INSPECTION_TRUNCATE_THRESHOLD
        head_chars = INSPECTION_HEAD_CHARS
        tail_chars = INSPECTION_TAIL_CHARS
    else:
        threshold = GENERAL_TRUNCATE_THRESHOLD
        head_chars = GENERAL_HEAD_CHARS
        tail_chars = GENERAL_TAIL_CHARS

    if original_length <= threshold:
        return content, False, original_length

    summary = _head_tail(content, head_chars, tail_chars)
    total_lines = content.count("\n") + 1
    return (
        "Output truncated to save LLM context. Showing the beginning and end "
        f"of {original_length:,} chars across {total_lines:,} lines."
        f"{paths_note}\n{summary}",
        True,
        original_length,
    )


def run_shell_command(
    command: str,
    description: str | None = None,
    is_background: bool = False,
    dir_path: str | None = None,
    timeout_ms: int = DEFAULT_TIMEOUT_MS,
    update_output: Callable[[str], None] | None = None,
    agent_state: AgentState | None = None,
) -> dict[str, Any]:
    """
    Executes a shell command.

    On Unix/Linux/macOS: Executes as `bash -c <command>`
    On Windows: Executes as `powershell.exe -NoProfile -Command <command>`

    The following information is returned:
    - Output: Combined stdout/stderr. Can be `(empty)` or partial on error.
    - Exit Code: Only included if non-zero (command failed).
    - Error: Only included if a process-level error occurred.
    - Signal: Only included if process was terminated by a signal.
    - Background PIDs: Only included if background processes were started.
    - Process Group PGID: Only included if available.

    Args:
        command: The exact command to execute
        description: Brief description of the command for the user
        dir_path: Directory to run the command in (optional)
        is_background: Whether to run in background
        timeout_ms: Timeout in milliseconds (0 for no timeout)
        update_output: Callback for streaming output updates

    Returns:
        Dict with content and returnDisplay matching gemini-cli format
    """
    try:
        # Validate command
        if not command or not command.strip():
            return {
                "content": "Command cannot be empty.",
                "returnDisplay": "Error: Empty command.",
                "error": {
                    "message": "Command cannot be empty.",
                    "type": "INVALID_COMMAND",
                },
            }

        sandbox: BaseSandbox = get_sandbox(agent_state)

        # Determine working directory (string resolution only; checks via sandbox)
        if dir_path:
            cwd = resolve_path(dir_path, sandbox)
            if not sandbox.file_exists(cwd):
                error_msg = f"Directory not found: {dir_path}"
                return {
                    "content": error_msg,
                    "returnDisplay": "Error: Directory not found.",
                    "error": {"message": error_msg, "type": "DIRECTORY_NOT_FOUND"},
                }
            info = sandbox.get_file_info(cwd)
            if not info.is_directory:
                error_msg = f"Path is not a directory: {dir_path}"
                return {
                    "content": error_msg,
                    "returnDisplay": "Error: Path is not a directory.",
                    "error": {"message": error_msg, "type": "NOT_A_DIRECTORY"},
                }
        else:
            cwd = str(sandbox.work_dir)

        timeout_arg = timeout_ms if timeout_ms and timeout_ms > 0 else None

        if is_background:
            # Background mode: sandbox.execute_bash supports cwd and background params
            start = time.time()
            cmd_result = sandbox.execute_bash(
                command,
                timeout=timeout_arg,
                cwd=cwd,
                background=True,
            )
            duration_ms = int((time.time() - start) * 1000)
            bg_pid = cmd_result.background_pid
            if bg_pid is not None:
                log_note = _output_paths_note(cmd_result.stdout_file, cmd_result.stderr_file)
                llm_content = (
                    f"Background task started (pid: {bg_pid}). "
                    "Use run_shell_command with ps/tail/kill on this pid and the reported log files; "
                    "do not wait with long sleep commands unless the task explicitly requires it."
                    f"{log_note}"
                )
                bg_result: dict[str, Any] = {
                    "content": llm_content,
                    "returnDisplay": f"Background task started (pid: {bg_pid})",
                    "duration_ms": duration_ms,
                    "backgroundPids": [bg_pid],
                }
                if cmd_result.output_dir:
                    bg_result["output_dir"] = cmd_result.output_dir
                    bg_result["stdout_file"] = cmd_result.stdout_file
                    bg_result["stderr_file"] = cmd_result.stderr_file
                return bg_result
            # Fallback if sandbox didn't return pid
            fallback_result: dict[str, Any] = {
                "content": cmd_result.stdout or "Background task started.",
                "returnDisplay": cmd_result.stdout or "Background task started.",
                "duration_ms": duration_ms,
            }
            if cmd_result.error:
                fallback_result["error"] = {
                    "message": cmd_result.error,
                    "type": "SHELL_EXECUTE_ERROR",
                }
            return fallback_result

        # Foreground mode
        # Build description for display
        cmd_description = command
        if dir_path:
            cmd_description += f" [in {dir_path}]"
        else:
            cmd_description += f" [current working directory {cwd}]"
        if description:
            cmd_description += f" ({description.replace(chr(10), ' ')})"
        # Streaming output is not supported by execute_bash; ignore update_output.
        _ = update_output

        # Execute command through sandbox, optionally scoping to directory via `cd`.
        cmd_to_run = command
        if cwd:
            cmd_to_run = f"cd {shlex.quote(cwd)} && {command}"

        start = time.time()
        cmd_result = sandbox.execute_bash(cmd_to_run, timeout=timeout_arg)
        duration_ms = int((time.time() - start) * 1000)

        stdout = cmd_result.stdout or ""
        stderr = cmd_result.stderr or ""
        output = stdout
        if stderr:
            output = f"{stdout}\n{stderr}" if stdout else stderr

        exit_code = cmd_result.exit_code
        error_message = cmd_result.error

        # Keep full logs on disk but compact what is sent back into the LLM.
        output, output_compacted, original_output_length = _format_shell_output(
            output,
            command=command,
            exit_code=exit_code,
            stdout_file=cmd_result.stdout_file,
            stderr_file=cmd_result.stderr_file,
        )

        # Build result
        llm_parts: list[str] = []
        if cmd_result.status == SandboxStatus.TIMEOUT:
            timeout_minutes = (timeout_ms / 60000) if timeout_ms else 0
            llm_parts.append(f"Timeout: command timed out after {timeout_minutes:.1f} minutes.")
        else:
            llm_parts.append(f"Output: {output if output else '(empty)'}")

        if error_message:
            llm_parts.append(f"Error: {error_message}")

        if exit_code != 0:
            llm_parts.append(f"Exit Code: {exit_code}")

        llm_content = "\n".join(llm_parts)

        # Build return display
        if output and output.strip():
            return_display = output
        elif cmd_result.status == SandboxStatus.TIMEOUT:
            return_display = f"Command timed out after {timeout_ms / 60000:.1f} minutes."
        elif error_message:
            return_display = f"Command failed: {error_message}"
        elif exit_code != 0:
            return_display = f"Command exited with code: {exit_code}"
        else:
            return_display = "(empty)"

        result: dict[str, Any] = {
            "content": llm_content,
            "returnDisplay": return_display,
            "duration_ms": duration_ms,
            "exit_code": exit_code,
        }

        # Include CommandResult truncation metadata and file paths
        if cmd_result.output_dir:
            result["output_dir"] = cmd_result.output_dir
            result["stdout_file"] = cmd_result.stdout_file
            result["stderr_file"] = cmd_result.stderr_file
        if output_compacted or cmd_result.truncated:
            result["truncated"] = True
        if output_compacted:
            result["original_output_length"] = original_output_length
        if cmd_result.truncated:
            result["original_stdout_length"] = cmd_result.original_stdout_length
            result["original_stderr_length"] = cmd_result.original_stderr_length

        if error_message or cmd_result.status in (
            SandboxStatus.ERROR,
            SandboxStatus.TIMEOUT,
        ):
            result["error"] = {
                "message": error_message or "Command failed",
                "type": "SHELL_EXECUTE_ERROR",
            }

        return result

    except Exception as e:
        error_msg = f"Error executing shell command: {str(e)}"
        return {
            "content": error_msg,
            "returnDisplay": error_msg,
            "error": {
                "message": error_msg,
                "type": "SHELL_EXECUTE_ERROR",
            },
        }
