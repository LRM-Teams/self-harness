"""Harbor adapter that runs Pi with a custom OpenAI-compatible model in E2B."""

from __future__ import annotations

import json
import os
import shlex
from pathlib import Path
from typing import Any

import yaml
from harbor.agents.base import BaseAgent
from harbor.environments.base import BaseEnvironment
from harbor.models.agent.context import AgentContext


_REMOTE_ROOT = Path("/installed-agent/pi")
_EVENTS_FILE = Path("/logs/agent/pi-events.jsonl")


class PiOutputLimitError(RuntimeError):
    """Pi stopped because the configured model output limit was exhausted."""


def _message_text(message: dict[str, Any]) -> str:
    content = message.get("content", "")
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    return "".join(
        block.get("text", "")
        for block in content
        if isinstance(block, dict) and block.get("type") == "text"
    )


def _clean_messages(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = []
    seen: set[str] = set()
    for event in events:
        if event.get("type") != "message_end":
            continue
        message = event.get("message")
        if not isinstance(message, dict):
            continue
        role = message.get("role")
        if role not in {"system", "user", "assistant", "toolResult"}:
            continue
        normalized: dict[str, Any] = {
            "role": "tool" if role == "toolResult" else role,
            "content": _message_text(message),
        }
        if role == "assistant":
            calls = [
                {
                    "id": block.get("id", ""),
                    "name": block.get("name", ""),
                    "type": "tool",
                    "input": block.get("arguments", {}),
                }
                for block in message.get("content", [])
                if isinstance(block, dict) and block.get("type") == "toolCall"
            ]
            if calls:
                normalized["tool_calls"] = calls
        key = json.dumps(normalized, sort_keys=True, ensure_ascii=False)
        if key not in seen:
            messages.append(normalized)
            seen.add(key)
    return messages


class PiAgent(BaseAgent):
    """Install and execute Pi inside Harbor's task environment."""

    SUPPORTS_ATIF = False

    def __init__(
        self,
        logs_dir: Path,
        model_name: str | None = None,
        config_path: str | Path | None = None,
        provider: str = "lenovo-deepseek-v4-flash",
        base_url: str | None = None,
        api_key_env: str = "LLM_API_KEY",
        ca_cert_path: str | Path | None = None,
        local_node_path: str | Path | None = None,
        local_pi_package_dir: str | Path | None = None,
        version: str = "0.84.1",
        *args: Any,
        **kwargs: Any,
    ) -> None:
        super().__init__(logs_dir=logs_dir, model_name=model_name, *args, **kwargs)
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        if config_path is None:
            raise ValueError("config_path is required for PiAgent")
        self._config_path = Path(config_path).resolve()
        self._config_dir = self._config_path.parent
        self._config = yaml.safe_load(self._config_path.read_text(encoding="utf-8")) or {}
        self._provider = str(self._config.get("provider") or provider)
        self._base_url = str(base_url or os.environ.get("LLM_BASE_URL", ""))
        self._api_key_env = api_key_env
        self._ca_cert_path = Path(ca_cert_path).resolve() if ca_cert_path else None
        self._local_node_path = (
            Path(local_node_path).resolve() if local_node_path else None
        )
        self._local_pi_package_dir = (
            Path(local_pi_package_dir).resolve() if local_pi_package_dir else None
        )
        if bool(self._local_node_path) != bool(self._local_pi_package_dir):
            raise ValueError(
                "local_node_path and local_pi_package_dir must be provided together"
            )
        self._version = str(self._config.get("pi_version") or version)
        self._max_tokens = int(self._config.get("max_tokens", 8192))
        if self._max_tokens <= 0:
            raise ValueError("max_tokens must be a positive integer")
        extensions_dir = self._config_dir / "extensions"
        self._extensions = (
            sorted(path for path in extensions_dir.glob("*.ts") if path.is_file())
            if extensions_dir.is_dir()
            else []
        )
        configured_tools = self._config.get("tools", [])
        self._tools = [str(tool) for tool in configured_tools] if configured_tools else []

    @staticmethod
    def name() -> str:
        return "pi"

    def version(self) -> str | None:
        return self._version

    def _model_id(self) -> str:
        if not self.model_name:
            return str(self._config.get("model") or "")
        return self.model_name.split("/", 1)[-1]

    def _write_runtime_files(self) -> tuple[Path, Path]:
        runtime_dir = self.logs_dir / "runtime"
        runtime_dir.mkdir(parents=True, exist_ok=True)
        models_path = runtime_dir / "models.json"
        settings_path = runtime_dir / "settings.json"
        models_path.write_text(
            json.dumps(
                {
                    "providers": {
                        self._provider: {
                            "baseUrl": self._base_url,
                            "api": "openai-completions",
                            "apiKey": "$PI_DEEPSEEK_API_KEY",
                            "authHeader": True,
                            "compat": {
                                "supportsDeveloperRole": False,
                                "supportsReasoningEffort": False,
                                "supportsStore": False,
                                "maxTokensField": "max_tokens",
                            },
                            "models": [
                                {
                                    "id": self._model_id(),
                                    "name": self._model_id(),
                                    "reasoning": False,
                                    "input": ["text"],
                                    "contextWindow": 524288,
                                    "maxTokens": self._max_tokens,
                                    "cost": {
                                        "input": 0,
                                        "output": 0,
                                        "cacheRead": 0,
                                        "cacheWrite": 0,
                                    },
                                }
                            ],
                        }
                    }
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        settings_path.write_text(
            json.dumps(
                {
                    "defaultProvider": self._provider,
                    "defaultModel": self._model_id(),
                    "defaultProjectTrust": "always",
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        return models_path, settings_path

    async def setup(self, environment: BaseEnvironment) -> None:
        setup_dir = self.logs_dir / "setup"
        setup_dir.mkdir(parents=True, exist_ok=True)
        await environment.exec(command=f"mkdir -p {_REMOTE_ROOT}/config {_REMOTE_ROOT}/extensions")

        if self._local_node_path and self._local_pi_package_dir:
            if not self._local_node_path.is_file():
                raise FileNotFoundError(f"Local Node binary not found: {self._local_node_path}")
            if not self._local_pi_package_dir.is_dir():
                raise FileNotFoundError(
                    f"Local Pi package not found: {self._local_pi_package_dir}"
                )
            remote_runtime = _REMOTE_ROOT / "runtime"
            remote_node = remote_runtime / "bin/node"
            remote_package = remote_runtime / "pi-package"
            await environment.exec(
                command=f"mkdir -p {remote_runtime}/bin {remote_package}"
            )
            await environment.upload_file(self._local_node_path, str(remote_node))
            # Docker's cp semantics nest the source basename when the destination
            # already exists.  The trailing `/.` copies the package contents so
            # `dist/cli.js` always lands directly under `remote_package`.
            await environment.upload_dir(f"{self._local_pi_package_dir}/.", str(remote_package))
            result = await environment.exec(
                command=(
                    f"chmod +x {remote_node} && "
                    f"{remote_node} {remote_package}/dist/cli.js --version"
                )
            )
        else:
            install_script = self._config_dir / "install-pi.sh"
            if not install_script.is_file():
                raise FileNotFoundError(f"Pi install script not found: {install_script}")
            await environment.upload_file(install_script, f"{_REMOTE_ROOT}/install-pi.sh")
            result = await environment.exec(command=f"bash {_REMOTE_ROOT}/install-pi.sh")
        (setup_dir / "return-code.txt").write_text(str(result.return_code), encoding="utf-8")
        (setup_dir / "stdout.txt").write_text(result.stdout or "", encoding="utf-8")
        (setup_dir / "stderr.txt").write_text(result.stderr or "", encoding="utf-8")
        if result.return_code != 0:
            raise RuntimeError(f"Pi setup failed with exit code {result.return_code}")

        models_path, settings_path = self._write_runtime_files()
        await environment.upload_file(models_path, f"{_REMOTE_ROOT}/config/models.json")
        await environment.upload_file(settings_path, f"{_REMOTE_ROOT}/config/settings.json")

        prompt_path = self._config_dir / str(self._config.get("system_prompt", "systemprompt.md"))
        await environment.upload_file(prompt_path, f"{_REMOTE_ROOT}/systemprompt.md")
        for extension_path in self._extensions:
            await environment.upload_file(
                extension_path,
                f"{_REMOTE_ROOT}/extensions/{extension_path.name}",
            )
        if self._ca_cert_path:
            await environment.upload_file(self._ca_cert_path, f"{_REMOTE_ROOT}/ca.pem")

    async def run(
        self,
        instruction: str,
        environment: BaseEnvironment,
        context: AgentContext,
    ) -> None:
        api_key = os.environ.get(self._api_key_env, "")
        if not api_key:
            raise ValueError(f"{self._api_key_env} is required for PiAgent")
        if not self._base_url:
            raise ValueError("LLM_BASE_URL/base_url is required for PiAgent")

        env = {
            "PI_CODING_AGENT_DIR": f"{_REMOTE_ROOT}/config",
            "PI_DEEPSEEK_API_KEY": api_key,
            "SERPER_API_KEY": os.environ.get("SERPER_API_KEY", ""),
        }
        if self._ca_cert_path:
            env["NODE_EXTRA_CA_CERTS"] = f"{_REMOTE_ROOT}/ca.pem"

        extension_flags = " ".join(
            f"--extension {shlex.quote(str(_REMOTE_ROOT / 'extensions' / path.name))}"
            for path in self._extensions
        )
        tools_flag = (
            f"--tools {shlex.quote(','.join(self._tools))} " if self._tools else ""
        )
        pi_command = "pi"
        if self._local_node_path and self._local_pi_package_dir:
            pi_command = (
                f"{_REMOTE_ROOT}/runtime/bin/node "
                f"{_REMOTE_ROOT}/runtime/pi-package/dist/cli.js"
            )
        command = (
            "set -o pipefail; "
            f"{pi_command} --mode json --no-session --approve "
            "--no-extensions --no-skills --no-prompt-templates "
            f"{extension_flags} "
            f"{tools_flag}"
            f"--provider {shlex.quote(self._provider)} "
            f"--model {shlex.quote(self._model_id())} "
            f"--system-prompt \"$(cat {_REMOTE_ROOT}/systemprompt.md)\" "
            f"{shlex.quote(instruction)} "
            f"| tee {_EVENTS_FILE}"
        )
        result = await environment.exec(command=command, env=env)
        events_path = self.logs_dir / "pi-events.jsonl"
        # Docker bind-mounts /logs/agent to self.logs_dir.  `tee` therefore
        # preserves partial Pi output even when Harbor cancels this coroutine
        # at the task timeout.  Remote environments still fall back to the
        # captured stdout after a normal return.
        if not events_path.exists():
            events_path.write_text(result.stdout or "", encoding="utf-8")
        events_text = events_path.read_text(encoding="utf-8", errors="replace")
        (self.logs_dir / "pi-stderr.txt").write_text(result.stderr or "", encoding="utf-8")
        (self.logs_dir / "pi-return-code.txt").write_text(str(result.return_code), encoding="utf-8")

        events: list[dict[str, Any]] = []
        for line in events_text.splitlines():
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(item, dict):
                events.append(item)
        messages = _clean_messages(events)
        trace = {"trace_id": self.logs_dir.parent.name, "messages": messages}
        (self.logs_dir / "nexau_in_memory_tracer.cleaned.json").write_text(
            json.dumps(trace, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

        final_stop_reason = ""
        for event in reversed(events):
            if event.get("type") != "agent_end":
                continue
            event_messages = event.get("messages", [])
            if not isinstance(event_messages, list):
                break
            for message in reversed(event_messages):
                if isinstance(message, dict) and message.get("role") == "assistant":
                    final_stop_reason = str(message.get("stopReason") or "")
                    usage = message.get("usage") or {}
                    context.n_input_tokens = int(usage.get("input", 0) or 0)
                    context.n_output_tokens = int(usage.get("output", 0) or 0)
                    context.n_cache_tokens = int(usage.get("cacheRead", 0) or 0)
                    break
            break

        if result.return_code != 0:
            raise RuntimeError(f"Pi exited with code {result.return_code}")
        if final_stop_reason == "length":
            raise PiOutputLimitError(
                f"Pi exhausted the configured {self._max_tokens}-token output limit"
            )
