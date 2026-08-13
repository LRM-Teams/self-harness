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
- Debug agent: one read-only local Pi subprocess per staged task
- Explore agent: one read-only local Pi research subprocess in iteration 1
- Evolution agent: one restricted local Pi subprocess per evolution attempt
- Model: `lenovo-deepseek-v4-flash/DeepSeek-V4-Flash-0731`
- Search: `serper_search` for Code, Explore, and Evolution, implemented as a Pi extension
- E2B concurrency: 4
- Feedback policy: reward-only; Pi sees copied agent traces and safe aggregate
  metadata, never raw verifier output or hidden tests

The old NexAU-specific auxiliary agents are bypassed in this experiment. All
four active roles use Pi and have separate system prompts:

```text
Code      agents/pi_code_agent/systemprompt.md
Debug     agents/pi_debug_agent/systemprompt.md
Explore   agents/pi_explore_agent/systemprompt.md
Evolution agents/pi_evolve_agent/systemprompt.md
```

Every invocation uses `--no-session`. Each benchmark task has its own E2B
sandbox and Pi process; every Debug task, Explore run, and Evolution run gets a
separate local Pi process and output directory. Roles exchange only persisted,
policy-controlled artifacts, never conversation state.

The iteration flow is:

```text
Pi Explore ────────────────┐
                          ├─> Pi Debug (per task) ─> Pi Evolution
Pi Code in Harbor/E2B ─────┘       ^                     |
        └─> reward-only traces ─────┘                     └─> next harness
```

Explore and Harbor run concurrently in iteration 1. Debug can read only one
task's staged agent traces per session. Evolution can read the workspace,
Explore report, Debug reports, aggregate analysis, and sanitized traces. Only
Evolution may modify the harness workspace.

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

## Generic candidate evolution

The evaluator-agnostic population runner in `evolution/` adds three independent
Pi search lanes, a persistent candidate graph, progressive
exploration/exploitation, post-evaluation debugging, and retrospective
cross-branch memory. See `docs/pi-candidate-evolution.md` and
`configs/experiments/exp-pi-candidate-evolution-example.yaml`.

The CO-Bench specialization adds a leak-resistant official evaluator adapter,
64-candidate scheduling, CO-specific prompts, and isolated single-CPU Docker
evaluation. See `docs/pi-cobench-evolution.md` and
`configs/experiments/exp-pi-cobench.yaml`.
