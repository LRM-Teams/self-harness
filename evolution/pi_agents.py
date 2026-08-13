from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from agents.pi_runtime import run_pi_agent

from .interfaces import ProductionContext
from .models import CandidateRecord, ProductionResult


PROJECT_DIR = Path(__file__).resolve().parent.parent
_FEATURES_RE = re.compile(r"^FEATURES:\s*(.+)$", re.MULTILINE | re.IGNORECASE)


@dataclass(frozen=True)
class PiEvolutionSettings:
    model: str
    base_url: str
    api_key: str
    candidate_entrypoint: str
    ca_cert_path: Path | None = None
    timeout_seconds: float = 900
    enable_search: bool = False
    producer_prompt_path: Path = PROJECT_DIR / "agents" / "pi_candidate_evolution" / "producer.md"
    debugger_prompt_path: Path = PROJECT_DIR / "agents" / "pi_candidate_evolution" / "debugger.md"
    communicator_prompt_path: Path = PROJECT_DIR / "agents" / "pi_candidate_evolution" / "communicator.md"


class PiCandidateProducer:
    def __init__(self, archive_root: Path, settings: PiEvolutionSettings):
        self.archive_root = archive_root.resolve()
        self.settings = settings

    def produce(self, context: ProductionContext) -> ProductionResult:
        parent_lines = [f"- `{path}`" for path in context.parent_dirs] or ["- none; start from scratch"]
        reference_lines = [f"- `{path}`" for path in context.reference_dirs] or ["- none"]
        query = "\n".join(
            [
                f"Create candidate `{context.record.candidate_id}` using the `{context.plan.operator}` operator.",
                f"Problem specification: `{context.problem_path}`",
                f"Writable candidate directory: `{context.artifact_dir}`",
                f"Required candidate entrypoint: `{context.artifact_dir / self.settings.candidate_entrypoint}`",
                f"Lane rationale: {context.plan.rationale}",
                "The first parent, when present, has already been copied into the writable directory.",
                "Parent snapshots:",
                *parent_lines,
                "Read-only cross-branch references:",
                *reference_lines,
                f"Shared retrospective memory: `{context.memory_path}`",
                "Do not edit parents or references. Work only inside the writable candidate directory.",
                "Return a concise summary ending with `FEATURES: comma-separated algorithm mechanisms`.",
            ]
        )
        tools = ["read", "write", "edit"]
        if self.settings.enable_search:
            tools.append("serper_search")
        result = run_pi_agent(
            query=query,
            cwd=self.archive_root,
            output_dir=self.archive_root / "sessions" / "produce" / context.record.candidate_id,
            system_prompt_path=self.settings.producer_prompt_path,
            prompt_context={
                "candidate_entrypoint": self.settings.candidate_entrypoint,
                "operator": context.plan.operator,
                "lane": context.plan.lane,
            },
            model=self.settings.model,
            base_url=self.settings.base_url,
            api_key=self.settings.api_key,
            ca_cert_path=self.settings.ca_cert_path,
            read_roots=[
                context.problem_path,
                context.memory_path,
                context.artifact_dir,
                *context.parent_dirs,
                *context.reference_dirs,
            ],
            write_roots=[context.artifact_dir],
            tools=tools,
            timeout_seconds=self.settings.timeout_seconds,
        )
        entrypoint = context.artifact_dir / self.settings.candidate_entrypoint
        if not entrypoint.is_file():
            raise FileNotFoundError(f"Pi did not create required entrypoint: {entrypoint}")
        features: tuple[str, ...] = ()
        match = _FEATURES_RE.search(result.text)
        if match:
            features = tuple(item.strip() for item in match.group(1).split(",") if item.strip())
        return ProductionResult(summary=result.text.strip(), features=features)


class PiCandidateDebugger:
    def __init__(self, archive_root: Path, settings: PiEvolutionSettings):
        self.archive_root = archive_root.resolve()
        self.settings = settings

    def debug(self, record: CandidateRecord, artifact_dir: Path) -> str:
        query = "\n".join(
            [
                f"Diagnose candidate `{record.candidate_id}` from generation {record.generation}.",
                f"Candidate directory: `{artifact_dir}`",
                f"Operator/lane: {record.operator}/{record.lane}",
                f"Parents: {record.parent_ids or ['none']}",
                f"Development score: {record.score}",
                f"Feasible: {record.feasible}",
                f"Runtime seconds: {record.runtime_seconds}",
                f"Local validation: {record.validation_feedback or 'no feedback'}",
                f"Evaluator feedback: {record.feedback or 'no feedback'}",
                f"Error type: {record.error_type or 'none'}",
                "Separate contract/feasibility bugs, algorithm-quality limitations, and runtime bottlenecks.",
                "End with one smallest high-value next experiment and reusable lessons.",
            ]
        )
        result = run_pi_agent(
            query=query,
            cwd=self.archive_root,
            output_dir=self.archive_root / "sessions" / "debug" / record.candidate_id,
            system_prompt_path=self.settings.debugger_prompt_path,
            prompt_context={},
            model=self.settings.model,
            base_url=self.settings.base_url,
            api_key=self.settings.api_key,
            ca_cert_path=self.settings.ca_cert_path,
            read_roots=[artifact_dir],
            write_roots=[],
            tools=["read"],
            timeout_seconds=self.settings.timeout_seconds,
        )
        return result.text.strip()


class PiExperienceCommunicator:
    def __init__(self, archive_root: Path, settings: PiEvolutionSettings):
        self.archive_root = archive_root.resolve()
        self.settings = settings

    def exchange(self, records: list[CandidateRecord], memory_path: Path) -> str:
        cards = []
        for record in records:
            cards.append(
                json.dumps(
                    {
                        "candidate_id": record.candidate_id,
                        "lane": record.lane,
                        "operator": record.operator,
                        "parents": record.parent_ids,
                        "score": record.score,
                        "feasible": record.feasible,
                        "runtime_seconds": record.runtime_seconds,
                        "features": record.features,
                        "summary": record.summary,
                        "debug_report": record.debug_report,
                    },
                    ensure_ascii=False,
                )
            )
        generation = max(item.generation for item in records)
        query = "\n".join(
            [
                f"Curate cross-branch experience after generation {generation}.",
                f"Existing memory: `{memory_path}`",
                "Candidate result cards:",
                *cards,
                "Extract only evidence-backed reusable mechanisms, failed approaches, and diversity guidance.",
                "Do not select a winner by prose; evaluator scores remain authoritative.",
            ]
        )
        result = run_pi_agent(
            query=query,
            cwd=self.archive_root,
            output_dir=self.archive_root / "sessions" / "exchange" / f"generation-{generation:03d}",
            system_prompt_path=self.settings.communicator_prompt_path,
            prompt_context={},
            model=self.settings.model,
            base_url=self.settings.base_url,
            api_key=self.settings.api_key,
            ca_cert_path=self.settings.ca_cert_path,
            read_roots=[memory_path],
            write_roots=[],
            tools=["read"],
            timeout_seconds=self.settings.timeout_seconds,
        )
        return result.text.strip()
