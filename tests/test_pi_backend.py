import json
import asyncio
from pathlib import Path
from types import SimpleNamespace

import evolve
from agents.pi_harbor_agent import PiAgent, _clean_messages
from agents.pi_e2b_environment import PiE2BEnvironment
from harbor.models.agent.context import AgentContext
from harbor.models.task.config import EnvironmentConfig as TaskEnvironmentConfig
from harbor.models.trial.paths import TrialPaths


def test_pi_events_convert_to_ahe_clean_trace() -> None:
    events = [
        {
            "type": "message_end",
            "message": {"role": "user", "content": "task"},
        },
        {
            "type": "message_end",
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "checking"},
                    {
                        "type": "toolCall",
                        "id": "call-1",
                        "name": "bash",
                        "arguments": {"command": "pwd"},
                    },
                ],
            },
        },
    ]

    messages = _clean_messages(events)

    assert messages[0] == {"role": "user", "content": "task"}
    assert messages[1]["content"] == "checking"
    assert messages[1]["tool_calls"][0]["name"] == "bash"


def test_pi_runtime_model_config_never_contains_secret(tmp_path: Path) -> None:
    config_dir = tmp_path / "agent"
    config_dir.mkdir()
    (config_dir / "pi_agent.yaml").write_text(
        "provider: example-provider\nmodel: example-model\npi_version: 0.84.1\n",
        encoding="utf-8",
    )
    agent = PiAgent(
        logs_dir=tmp_path / "logs",
        model_name="example-provider/example-model",
        config_path=config_dir / "pi_agent.yaml",
        base_url="https://example.invalid/v1",
    )

    models_path, _ = agent._write_runtime_files()
    model_config = models_path.read_text(encoding="utf-8")

    assert "$PI_DEEPSEEK_API_KEY" in model_config
    assert "actual-secret" not in model_config


def test_pi_harbor_run_uses_pinned_tools_and_writes_trace(
    tmp_path: Path, monkeypatch
) -> None:
    project = Path(evolve.__file__).resolve().parent
    agent = PiAgent(
        logs_dir=tmp_path / "logs",
        model_name="lenovo-deepseek-v4-flash/DeepSeek-V4-Flash-0731",
        config_path=project / "agents" / "pi_code_agent" / "pi_agent.yaml",
        base_url="https://example.invalid/v1",
    )
    monkeypatch.setenv("LLM_API_KEY", "actual-secret")

    class Result:
        return_code = 0
        stderr = ""
        stdout = "\n".join(
            [
                json.dumps(
                    {
                        "type": "message_end",
                        "message": {"role": "user", "content": "task"},
                    }
                ),
                json.dumps(
                    {
                        "type": "message_end",
                        "message": {
                            "role": "assistant",
                            "content": [{"type": "text", "text": "done"}],
                        },
                    }
                ),
            ]
        )

    class Environment:
        command = ""
        env = {}

        async def exec(self, *, command, env):
            self.command = command
            self.env = env
            return Result()

    environment = Environment()
    asyncio.run(agent.run("do the task", environment, AgentContext()))

    assert "--tools read,bash,edit,write,serper_search" in environment.command
    assert "--extension /installed-agent/pi/extensions/serper.ts" in environment.command
    assert "actual-secret" not in environment.command
    assert environment.env["PI_DEEPSEEK_API_KEY"] == "actual-secret"
    trace = json.loads(
        (tmp_path / "logs" / "nexau_in_memory_tracer.cleaned.json").read_text()
    )
    assert trace["messages"][-1] == {"role": "assistant", "content": "done"}


def test_harbor_command_uses_custom_pi_import_path(tmp_path: Path) -> None:
    project = Path(evolve.__file__).resolve().parent
    config = evolve.load_config(
        str(project / "configs" / "experiments" / "exp-pi-deepseek-v4-flash.yaml")
    )
    command = evolve._build_harbor_cmd(
        config,
        project / "agents" / "pi_code_agent",
        "pi_agent.yaml",
        tmp_path,
    )

    assert command[:3] == ["harbor", "run", "--config"]
    job_config = json.loads(Path(command[3]).read_text(encoding="utf-8"))
    assert job_config["agents"][0]["import_path"] == "agents.pi_harbor_agent:PiAgent"
    assert (
        job_config["environment"]["import_path"]
        == "agents.pi_e2b_environment:PiE2BEnvironment"
    )
    assert job_config["environment"]["kwargs"]["template_prefix"] == "ahe-pi-v1"
    assert job_config["orchestrator"]["n_concurrent_trials"] == 4


def test_pi_e2b_environment_uses_separate_alias_namespace(tmp_path: Path) -> None:
    environment_dir = tmp_path / "environment"
    environment_dir.mkdir()
    (environment_dir / "Dockerfile").write_text("FROM ubuntu:24.04\n", encoding="utf-8")
    environment = PiE2BEnvironment(
        environment_dir=environment_dir,
        environment_name="task.with.dots",
        session_id="test-session",
        trial_paths=TrialPaths(tmp_path / "trial"),
        task_env_config=TaskEnvironmentConfig(),
        template_prefix="ahe-pi-v1",
    )

    assert environment._template_name == "ahe-pi-v1-task-with-dots"


def test_pi_reward_only_staging_copies_only_agent_trace(tmp_path: Path) -> None:
    job_dir = tmp_path / "job"
    trial_dir = job_dir / "sample-task__abc1234"
    agent_dir = trial_dir / "agent"
    verifier_dir = trial_dir / "verifier"
    agent_dir.mkdir(parents=True)
    verifier_dir.mkdir()
    (agent_dir / "nexau_in_memory_tracer.cleaned.json").write_text(
        json.dumps({"trace_id": "t1", "messages": [{"role": "user", "content": "task"}]}),
        encoding="utf-8",
    )
    (verifier_dir / "reward.txt").write_text("0", encoding="utf-8")
    (verifier_dir / "test-stdout.txt").write_text("HIDDEN VERIFIER CONTENT", encoding="utf-8")
    (trial_dir / "result.json").write_text(
        json.dumps({"finished_at": "now", "exception_info": None}),
        encoding="utf-8",
    )
    iteration_dir = tmp_path / "iteration_001"

    evolve._prepare_pi_reward_only_inputs(
        config={
            "agent_debugger": {
                "enabled": False,
                "feedback_mode": "reward_only",
                "max_tasks": 10,
            }
        },
        job_dir=job_dir,
        task_results={"sample-task": "fail"},
        iteration_dir=iteration_dir,
        iteration=1,
    )

    staged = list((iteration_dir / "input" / "sanitized_feedback" / "traces").rglob("*.json"))
    assert len(staged) == 1
    assert "HIDDEN VERIFIER CONTENT" not in staged[0].read_text(encoding="utf-8")
    assert "HIDDEN VERIFIER CONTENT" not in (
        iteration_dir / "input" / "analysis" / "overview.md"
    ).read_text(encoding="utf-8")


def test_pi_config_enables_all_four_role_specific_agents() -> None:
    project = Path(evolve.__file__).resolve().parent
    config = evolve.load_config(
        str(project / "configs" / "experiments" / "exp-pi-deepseek-v4-flash.yaml")
    )

    assert config["agent_backend"] == "pi"
    assert config["explore_agent"]["enabled"] is True
    assert config["explore_agent"]["backend"] == "pi"
    assert config["agent_debugger"]["enabled"] is True
    assert config["agent_debugger"]["backend"] == "pi"
    for role in ("pi_code_agent", "pi_debug_agent", "pi_explore_agent", "pi_evolve_agent"):
        assert (project / "agents" / role / "systemprompt.md").is_file()


def test_pi_debug_uses_one_isolated_read_only_session_per_task(
    tmp_path: Path, monkeypatch
) -> None:
    exp_dir = tmp_path / "experiment"
    iteration_dir = exp_dir / "runs" / "iteration_001"
    bundle_dir = iteration_dir / "input" / "sanitized_feedback"
    manifest_jobs = []
    for task_name, verdict in (("failed-task", "FAIL"), ("passed-task", "PASS")):
        safe_id = evolve._safe_task_id(task_name)
        trace_path = bundle_dir / "traces" / safe_id / "trace01.json"
        trace_path.parent.mkdir(parents=True, exist_ok=True)
        trace_path.write_text(json.dumps({"messages": []}), encoding="utf-8")
        manifest_jobs.append({
            "task_name": task_name,
            "safe_id": safe_id,
            "verdicts": [verdict],
            "trace_paths": [str(trace_path.relative_to(exp_dir))],
            "process_metadata": [{}],
        })
    (bundle_dir / "manifest.json").write_text(
        json.dumps({"feedback_mode": "reward_only", "jobs": manifest_jobs}),
        encoding="utf-8",
    )

    calls = []

    def fake_run_pi_agent(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(text="ROOT CAUSE: evidence-based diagnosis")

    monkeypatch.setattr("agents.pi_runtime.run_pi_agent", fake_run_pi_agent)
    overview = evolve.run_parallel_pi_debug_agents(
        config={
            "llm": {"api_key": "secret", "base_url": "https://example.invalid", "model": "p/m"},
            "pi": {},
            "agent_debugger": {
                "enabled": True,
                "feedback_mode": "reward_only",
                "max_concurrent": 2,
                "timeout_per_task": 10,
            },
        },
        exp_dir=exp_dir,
        iteration_dir=iteration_dir,
        iteration=1,
    )

    assert len(calls) == 2
    assert len({call["output_dir"] for call in calls}) == 2
    assert all(call["tools"] == ["read"] for call in calls)
    assert all(call["write_roots"] == [] for call in calls)
    assert all(len(call["read_roots"]) == 1 for call in calls)
    assert all("pi_debug_agent" in str(call["system_prompt_path"]) for call in calls)
    assert "failed-task" in overview
    assert (iteration_dir / "input" / "analysis" / "pi-debug-complete.json").is_file()


def test_pi_explore_is_independent_read_only_and_serper_enabled(
    tmp_path: Path, monkeypatch
) -> None:
    exp_dir = tmp_path / "experiment"
    workspace_dir = exp_dir / "workspace"
    iteration_dir = exp_dir / "runs" / "iteration_001"
    workspace_dir.mkdir(parents=True)
    calls = []

    def fake_run_pi_agent(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(text="EXTERNAL EVIDENCE\n- primary source")

    monkeypatch.setattr("agents.pi_runtime.run_pi_agent", fake_run_pi_agent)
    evolve._run_pi_explore_agent(
        config={
            "llm": {"api_key": "secret", "base_url": "https://example.invalid", "model": "p/m"},
            "pi": {},
            "explore_agent": {"enabled": True, "timeout_minutes": 1},
        },
        exp_dir=exp_dir,
        workspace_dir=workspace_dir,
        iteration_dir=iteration_dir,
        iteration=1,
    )

    assert len(calls) == 1
    call = calls[0]
    assert call["tools"] == ["read", "serper_search"]
    assert call["write_roots"] == []
    assert workspace_dir in call["read_roots"]
    assert "pi_explore_agent" in str(call["system_prompt_path"])
    assert call["output_dir"] == iteration_dir / "pi_agents" / "explore"
    assert (iteration_dir / "input" / "explore" / "report.md").is_file()


def test_pi_evolution_reads_debug_explore_and_sanitized_inputs(
    tmp_path: Path, monkeypatch
) -> None:
    exp_dir = tmp_path / "experiment"
    iteration_dir = exp_dir / "runs" / "iteration_001"
    workspace_dir = exp_dir / "workspace"
    analysis_dir = iteration_dir / "input" / "analysis"
    bundle_dir = iteration_dir / "input" / "sanitized_feedback"
    explore_dir = iteration_dir / "input" / "explore"
    for path in (workspace_dir, analysis_dir, bundle_dir, explore_dir):
        path.mkdir(parents=True, exist_ok=True)
    calls = []

    def fake_run_pi_agent(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(text="evolved")

    monkeypatch.setattr("agents.pi_runtime.run_pi_agent", fake_run_pi_agent)
    result = evolve._run_pi_evolve_agent(
        config={
            "llm": {"api_key": "secret", "base_url": "https://example.invalid", "model": "p/m"},
            "pi": {},
            "agent_debugger": {"feedback_mode": "reward_only"},
        },
        exp_dir=exp_dir,
        iteration=1,
        query="improve",
        iteration_dir=iteration_dir,
    )

    assert result == "evolved"
    assert calls[0]["read_roots"] == [
        workspace_dir.resolve(),
        analysis_dir.resolve(),
        bundle_dir.resolve(),
        explore_dir.resolve(),
    ]
    assert calls[0]["write_roots"] == [workspace_dir.resolve()]
    assert calls[0]["output_dir"] == iteration_dir / "evolve"


def test_local_pi_runtime_always_disables_session_persistence(
    tmp_path: Path, monkeypatch
) -> None:
    from agents import pi_runtime

    captured = {}

    class FakeProcess:
        def __init__(self, command, **kwargs):
            captured["command"] = command
            captured["env"] = kwargs["env"]
            self.stdout = iter([
                json.dumps({
                    "type": "message_end",
                    "message": {"role": "assistant", "content": "done"},
                }) + "\n"
            ])

        def wait(self):
            return 0

        def kill(self):
            raise AssertionError("unexpected timeout")

    monkeypatch.setattr(pi_runtime.shutil, "which", lambda _: "/fake/pi")
    monkeypatch.setattr(pi_runtime.subprocess, "Popen", FakeProcess)
    prompt = tmp_path / "system.md"
    prompt.write_text("role prompt", encoding="utf-8")
    result = pi_runtime.run_pi_agent(
        query="work",
        cwd=tmp_path,
        output_dir=tmp_path / "session",
        system_prompt_path=prompt,
        prompt_context={},
        model="provider/model",
        base_url="https://example.invalid/v1",
        api_key="actual-secret",
        read_roots=[tmp_path],
        tools=["read"],
    )

    assert "--no-session" in captured["command"]
    assert "--no-extensions" in captured["command"]
    assert "serper.ts" not in " ".join(captured["command"])
    assert "actual-secret" not in " ".join(captured["command"])
    assert captured["env"]["PI_DEEPSEEK_API_KEY"] == "actual-secret"
    assert result.text == "done"
