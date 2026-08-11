"""Local Pi subprocess runner used by AHE meta-agents."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from jinja2 import Environment, StrictUndefined


PROJECT_DIR = Path(__file__).resolve().parent.parent


@dataclass
class PiRunResult:
    text: str
    returncode: int
    events_path: Path
    trace_path: Path


def _content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    return "".join(
        block.get("text", "")
        for block in content
        if isinstance(block, dict) and block.get("type") == "text"
    )


def _messages_from_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = []
    seen: set[str] = set()
    for event in events:
        if event.get("type") != "message_end" or not isinstance(event.get("message"), dict):
            continue
        message = event["message"]
        role = message.get("role")
        if role not in {"system", "user", "assistant", "toolResult"}:
            continue
        normalized = {
            "role": "tool" if role == "toolResult" else role,
            "content": _content_text(message.get("content")),
        }
        key = json.dumps(normalized, sort_keys=True, ensure_ascii=False)
        if key not in seen:
            messages.append(normalized)
            seen.add(key)
    return messages


def run_pi_agent(
    *,
    query: str,
    cwd: Path,
    output_dir: Path,
    system_prompt_path: Path,
    prompt_context: dict[str, Any],
    model: str,
    base_url: str,
    api_key: str,
    ca_cert_path: Path | None = None,
    read_roots: list[Path] | None = None,
    write_roots: list[Path] | None = None,
    write_files: list[Path] | None = None,
    tools: list[str] | None = None,
    timeout_seconds: float | None = None,
) -> PiRunResult:
    if "/" not in model:
        raise ValueError("Pi model must use provider/model format")
    provider, model_id = model.split("/", 1)
    pi_bin = os.environ.get("PI_BIN") or shutil.which("pi")
    if not pi_bin:
        raise RuntimeError("Pi CLI not found; set PI_BIN or install pi")

    output_dir.mkdir(parents=True, exist_ok=True)
    config_dir = output_dir / "pi-config"
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "models.json").write_text(
        json.dumps(
            {
                "providers": {
                    provider: {
                        "baseUrl": base_url,
                        "api": "openai-completions",
                        "apiKey": "$PI_DEEPSEEK_API_KEY",
                        "authHeader": True,
                        "compat": {
                            "supportsDeveloperRole": False,
                            "supportsReasoningEffort": False,
                            "supportsStore": False,
                            "maxTokensField": "max_tokens",
                        },
                        "models": [{"id": model_id, "contextWindow": 524288, "maxTokens": 4096}],
                    }
                }
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    (config_dir / "settings.json").write_text(
        json.dumps(
            {
                "defaultProvider": provider,
                "defaultModel": model_id,
                "defaultProjectTrust": "always",
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    template = Environment(undefined=StrictUndefined).from_string(
        system_prompt_path.read_text(encoding="utf-8")
    )
    system_prompt = template.render(**prompt_context)
    events_path = output_dir / "pi-events.jsonl"
    stderr_path = output_dir / "pi-stderr.txt"
    trace_path = output_dir / "nexau_in_memory_tracer.cleaned.json"

    env = os.environ.copy()
    env.update(
        {
            "PI_CODING_AGENT_DIR": str(config_dir),
            "PI_DEEPSEEK_API_KEY": api_key,
            "AHE_TOOL_READ_ROOTS": os.pathsep.join(str(p.resolve()) for p in (read_roots or [cwd])),
            "AHE_TOOL_WRITE_ROOTS": os.pathsep.join(str(p.resolve()) for p in (write_roots or [])),
            "AHE_TOOL_WRITE_FILES": os.pathsep.join(str(p.resolve()) for p in (write_files or [])),
        }
    )
    if ca_cert_path:
        env["NODE_EXTRA_CA_CERTS"] = str(ca_cert_path.resolve())

    enabled_tools = tools or ["read", "write", "edit", "serper_search"]
    unsupported_tools = set(enabled_tools) - {"read", "write", "edit", "serper_search"}
    if unsupported_tools:
        raise ValueError(f"Unsupported restricted Pi tools: {sorted(unsupported_tools)}")

    command = [
        pi_bin,
        "--mode", "json",
        "--no-session",
        "--approve",
        "--no-extensions",
        "--no-skills",
        "--no-prompt-templates",
        "--tools", ",".join(enabled_tools),
        "--extension", str(PROJECT_DIR / "agents" / "pi_extensions" / "visibility_guard.ts"),
    ]
    if "serper_search" in enabled_tools:
        command.extend(
            [
                "--extension",
                str(PROJECT_DIR / "agents" / "pi_code_agent" / "extensions" / "serper.ts"),
            ]
        )
    command.extend(
        [
            "--provider", provider,
            "--model", model_id,
            "--system-prompt", system_prompt,
            query,
        ]
    )

    events: list[dict[str, Any]] = []
    with events_path.open("w", encoding="utf-8") as out, stderr_path.open(
        "w", encoding="utf-8"
    ) as err:
        process = subprocess.Popen(
            command,
            cwd=cwd,
            env=env,
            stdout=subprocess.PIPE,
            stderr=err,
            text=True,
            bufsize=1,
        )
        timed_out = threading.Event()

        def _terminate_on_timeout() -> None:
            timed_out.set()
            process.kill()

        timer = None
        if timeout_seconds is not None and timeout_seconds > 0:
            timer = threading.Timer(timeout_seconds, _terminate_on_timeout)
            timer.daemon = True
            timer.start()
        try:
            assert process.stdout is not None
            for line in process.stdout:
                out.write(line)
                out.flush()
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(event, dict):
                    events.append(event)
            returncode = process.wait()
        finally:
            if timer is not None:
                timer.cancel()

    if timed_out.is_set():
        raise TimeoutError(f"Pi exceeded {timeout_seconds}s; see {stderr_path}")

    messages = _messages_from_events(events)
    trace_path.write_text(
        json.dumps({"trace_id": output_dir.name, "messages": messages}, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    final_text = next(
        (
            message["content"]
            for message in reversed(messages)
            if message.get("role") == "assistant" and message.get("content")
        ),
        "",
    )
    if returncode != 0:
        raise RuntimeError(f"Pi exited with code {returncode}; see {stderr_path}")
    return PiRunResult(final_text, returncode, events_path, trace_path)
