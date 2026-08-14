"""Docker environment with scoped host-proxy forwarding for Pi experiments."""

from __future__ import annotations

import os
from urllib.parse import urlsplit, urlunsplit

from harbor.environments.docker.docker import DockerEnvironment


_PROXY_NAMES = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY")


def _container_proxy_url(value: str, bridge_host: str) -> str:
    """Replace a loopback proxy host with the Docker bridge gateway."""
    try:
        parsed = urlsplit(value)
        if parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
            return value
        host = f"[{bridge_host}]" if ":" in bridge_host else bridge_host
        userinfo = ""
        if parsed.username is not None:
            userinfo = parsed.username
            if parsed.password is not None:
                userinfo += f":{parsed.password}"
            userinfo += "@"
        port = f":{parsed.port}" if parsed.port is not None else ""
        return urlunsplit(
            (parsed.scheme, f"{userinfo}{host}{port}", parsed.path, parsed.query, parsed.fragment)
        )
    except ValueError:
        return value


class PiDockerEnvironment(DockerEnvironment):
    """Forward host proxy settings only to internet-enabled task containers."""

    def __init__(
        self,
        *args,
        inherit_host_proxy: bool = True,
        docker_bridge_host: str = "172.17.0.1",
        no_proxy_append: list[str] | None = None,
        **kwargs,
    ) -> None:
        super().__init__(*args, **kwargs)
        self._pi_proxy_env: dict[str, str] = {}
        if not inherit_host_proxy or not self.task_env_config.allow_internet:
            return

        for name in _PROXY_NAMES:
            value = os.environ.get(name) or os.environ.get(name.lower())
            if not value:
                continue
            value = _container_proxy_url(value, docker_bridge_host)
            self._pi_proxy_env[name] = value
            self._pi_proxy_env[name.lower()] = value

        no_proxy = os.environ.get("NO_PROXY") or os.environ.get("no_proxy") or ""
        entries = [item.strip() for item in no_proxy.split(",") if item.strip()]
        for item in no_proxy_append or []:
            if item and item not in entries:
                entries.append(item)
        if entries:
            value = ",".join(entries)
            self._pi_proxy_env["NO_PROXY"] = value
            self._pi_proxy_env["no_proxy"] = value

    async def exec(
        self,
        command: str,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        timeout_sec: int | None = None,
    ):
        merged_env = dict(self._pi_proxy_env)
        if env:
            merged_env.update(env)
        return await super().exec(
            command=command,
            cwd=cwd,
            env=merged_env or None,
            timeout_sec=timeout_sec,
        )
