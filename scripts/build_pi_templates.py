#!/usr/bin/env python3
"""Build isolated, Pi-preinstalled E2B templates for an AHE task directory."""

from __future__ import annotations

import argparse
import asyncio
import os
import tomllib
from pathlib import Path

from e2b import AsyncTemplate, Template
from dotenv import load_dotenv


PI_VERSION = "0.84.1"
DEFAULT_PREFIX = "ahe-pi-v1"
PROJECT_DIR = Path(__file__).resolve().parent.parent

# Always bind template creation to this independent project's copied account.
load_dotenv(PROJECT_DIR / ".env", override=True)
load_dotenv(PROJECT_DIR / ".env.pi", override=True)


def parse_mem_mb(value: str) -> int:
    value = value.strip().upper()
    if value.endswith("G"):
        return int(float(value[:-1]) * 1024)
    if value.endswith("M"):
        return int(float(value[:-1]))
    return int(value)


def task_alias(task_dir: Path, prefix: str) -> str:
    return f"{prefix}-{task_dir.name.replace('.', '-')}"


def add_pi(tpl: Template) -> Template:
    install = (
        "export NVM_DIR=/root/.nvm"
        " && curl -fsSL https://raw.githubusercontent.com/nvm-sh/nvm/v0.40.2/install.sh | bash"
        ' && . "$NVM_DIR/nvm.sh"'
        " && nvm install 22"
        f" && npm install -g @earendil-works/pi-coding-agent@{PI_VERSION}"
        " && ln -sf \"$(command -v node)\" /usr/local/bin/node"
        " && ln -sf \"$(command -v npm)\" /usr/local/bin/npm"
        " && ln -sf \"$(command -v pi)\" /usr/local/bin/pi"
        " && pi --version"
    )
    return tpl.apt_install(["ca-certificates", "curl", "git"]).run_cmd(install)


def make_template(task_dir: Path, task_config: dict) -> Template:
    environment = task_config.get("environment", {})
    image = environment.get("docker_image")
    if image:
        template = (
            Template()
            .from_image(
                image=image,
                username=os.environ.get("DOCKER_REGISTRY_USERNAME"),
                password=os.environ.get("DOCKER_REGISTRY_PASSWORD"),
            )
            .set_user("root")
        )
    else:
        dockerfile = task_dir / "environment" / "Dockerfile"
        if not dockerfile.is_file():
            raise FileNotFoundError(f"No docker_image or Dockerfile for {task_dir.name}")
        template = Template().from_dockerfile(
            dockerfile_content_or_path=str(dockerfile)
        ).set_user("root")
    return add_pi(template)


async def build_one(task_dir: Path, prefix: str, force: bool) -> str:
    alias = task_alias(task_dir, prefix)
    if not force and Template.alias_exists(alias):
        print(f"SKIP {task_dir.name}: {alias} already exists", flush=True)
        return "skip"
    with (task_dir / "task.toml").open("rb") as handle:
        task_config = tomllib.load(handle)
    environment = task_config.get("environment", {})
    cpus = int(environment.get("cpus", 1))
    memory_mb = parse_mem_mb(str(environment.get("memory", "2G")))
    print(f"BUILD {task_dir.name}: alias={alias}", flush=True)
    info = await AsyncTemplate.build(
        template=make_template(task_dir, task_config),
        alias=alias,
        cpu_count=cpus,
        memory_mb=memory_mb,
    )
    print(f"DONE {task_dir.name}: id={info.template_id}", flush=True)
    return "ok"


async def async_main(args: argparse.Namespace) -> None:
    # Harbor's cache nests each task below an opaque content-addressed parent;
    # a manually downloaded dataset often places tasks directly under the root.
    # Supporting both layouts keeps template preparation reproducible.
    task_dirs = sorted(
        {path.parent for path in args.dataset_dir.rglob("task.toml")},
        key=lambda path: path.name,
    )
    if args.names:
        selected = set(args.names)
        task_dirs = [path for path in task_dirs if path.name in selected]
    if not task_dirs:
        raise SystemExit("No matching task directories")

    semaphore = asyncio.Semaphore(args.jobs)
    counts = {"ok": 0, "skip": 0, "fail": 0}

    async def worker(task_dir: Path) -> None:
        async with semaphore:
            try:
                status = await build_one(task_dir, args.prefix, args.force)
            except Exception as exc:
                status = "fail"
                print(f"FAIL {task_dir.name}: {exc}", flush=True)
            counts[status] += 1

    await asyncio.gather(*(worker(path) for path in task_dirs))
    print(f"SUMMARY {counts}", flush=True)
    if counts["fail"]:
        raise SystemExit(1)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset-dir", type=Path, required=True)
    parser.add_argument("names", nargs="*")
    parser.add_argument("-j", "--jobs", type=int, default=4)
    parser.add_argument("--prefix", default=DEFAULT_PREFIX)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    if not args.dataset_dir.is_dir():
        parser.error(f"dataset directory does not exist: {args.dataset_dir}")
    if args.jobs < 1:
        parser.error("--jobs must be >= 1")
    asyncio.run(async_main(args))


if __name__ == "__main__":
    main()
