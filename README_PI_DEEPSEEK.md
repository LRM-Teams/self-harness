# Independent Pi + DeepSeek experiment

This clone is intentionally independent from
`/home/jianghp3/agentic-harness-engineering`:

- project: `/home/jianghp3/agentic-harness-engineering-pi-deepseek`
- branch: `experiment/pi-deepseek`
- Python environment: this project's `.venv`
- experiment output: this project's `experiments/`
- credentials: local ignored files `.env`, `.env.pi`, and `.secrets/`

The original repository, experiment checkpoints, tmux sessions, worktree, and
virtual environment are not shared.

## Active agent stack

- Harbor/E2B code agent: custom `agents.pi_harbor_agent:PiAgent`
- Evolution agent: local Pi subprocess through `agents/pi_runtime.py`
- Model: `lenovo-deepseek-v4-flash/DeepSeek-V4-Flash-0731`
- Search: `serper_search`, implemented as a Pi extension
- E2B concurrency: 4
- Feedback policy: reward-only; Pi sees copied agent traces and safe aggregate
  metadata, never raw verifier output or hidden tests

The old NexAU-specific explore-agent and agent-debugger are disabled in this
experiment. Their useful roles are covered by Pi's Serper tool and AHE's native
sanitized trace staging, so every agent process that is active is Pi.

## Configuration

The experiment overlay is:

```text
configs/experiments/exp-pi-deepseek-v4-flash.yaml
```

The evolvable Pi code-agent harness is:

```text
agents/pi_code_agent/
├── pi_agent.yaml
├── systemprompt.md
├── install-pi.sh
└── extensions/serper.ts
```

The E2B adapter installs the pinned Pi `0.84.1`, uploads a credential-free
`models.json`, forwards the model key and Serper key only through environment
variables, and writes a cleaned AHE-compatible trace after each task.

Pi uses the separate E2B alias namespace `ahe-pi-v1-*`; it never overwrites or
attaches to the original experiment's templates. Prebuild those templates after
the original run releases its E2B slots:

```bash
cd /home/jianghp3/agentic-harness-engineering-pi-deepseek
uv run python scripts/build_pi_templates.py \
  --dataset-dir /home/jianghp3/.cache/harbor/tasks \
  -j 4
```

If a prefixed template has not yet been built, Harbor can still create its base
template and the agent setup will install Pi on first use; prebuilding avoids
repeating that installation across trials.

## Run

Do not start this while another experiment is consuming the same E2B account's
full concurrency allowance.

```bash
cd /home/jianghp3/agentic-harness-engineering-pi-deepseek
uv run python evolve.py \
  --config configs/experiments/exp-pi-deepseek-v4-flash.yaml
```

## Checks

```bash
cd /home/jianghp3/agentic-harness-engineering-pi-deepseek
PYTHONPATH=. .venv/bin/pytest -q tests/test_pi_backend.py
```

The preflight has verified the isolated Pi-to-DeepSeek request, Serper, the
Lenovo CA, reward-only path blocking, and the copied E2B credential. It also
built `ahe-pi-v1-mteb-leaderboard` and completed a real temporary E2B
`PiAgent.setup -> Pi -> DeepSeek` request; the smoke sandbox was destroyed
afterward.
