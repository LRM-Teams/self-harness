# Current AHE Reproduction Notes (Kai, 2026-07-30)

This branch vendors the current Agentic Harness Engineering code used for the #self-harness TB2 experiments, plus Kai's current harness patch.

## Source and version pins

- Target repo: `https://github.com/LRM-Teams/self-harness.git`
- Imported upstream worktree: `agentic-harness-engineering` at commit `e7da40d0850826672a4eeb8624aede78587cf190`
- Local branch used for this handoff: `agent/kai/ahe-providerfix-verifiernorm`
- Fixed model for the measured runs: `gpt-5.5`
- Benchmark: `terminal-bench@2.0` via Harbor/NexAU on E2B

## Agent entry and harness config

- NexAU entry manifest: `experiments/evolved_harness/nexau.json`
- Agent config used by default: `experiments/evolved_harness/code_agent.yaml`
- Provider-compatible gpt-5.5 config: `experiments/evolved_harness/code_agent.gpt55-openai-chat.yaml`
- Agent code entry: `experiments/evolved_harness/start.py`
- System prompt under test: `experiments/evolved_harness/systemprompt.md`
- Execution-risk middleware: `experiments/evolved_harness/middleware/execution_risk_hints.py`

Required non-secret environment variables:

```bash
export LLM_API_KEY=...
export LLM_BASE_URL=...
export LLM_MODEL=gpt-5.5
export E2B_API_KEY=...
# Optional for self-hosted E2B:
# export E2B_API_URL=...
# export E2B_DOMAIN=...
```

## Template/image preflight

Harbor uses `terminal-bench@2.0` task images/templates in E2B. Build or retry templates before a clean full run:

```bash
uv sync
uv run python scripts/build_templates.py --dataset-dir /path/to/harbor-datasets/terminal-bench-2 -j 16
uv run python scripts/build_templates.py --dataset-dir /path/to/harbor-datasets/terminal-bench-2 --retry-failed
```

`build_templates.py` now uses the E2B connection config without forcing a separate access-token path, which matches the sandbox environment used in the latest runs.

## Clean full-run command shape

Use a fresh job name and a provider-compatible config. Keep concurrency under the E2B account/cluster cap.

```bash
E2B_SANDBOX_TIMEOUT=3599 uv run harbor run \
  --job-name kai-cleanfull-gpt55-$(date -u +%Y%m%d-%H%M%S) \
  --jobs-dir jobs/kai-cleanfull-gpt55 \
  --n-concurrent 6 \
  --env e2b \
  --agent nexau \
  --model gpt-5.5 \
  --agent-kwarg config_path=experiments/evolved_harness/code_agent.gpt55-openai-chat.yaml \
  --dataset terminal-bench@2.0
```

For timeout experiments, change exactly one harness knob and run the target slice first, then a clean full run only after the slice is healthy.

## Latest measured score ledger

Keep these ledgers separate:

- Baseline full run `kai-verifiernorm-zz-gpt55-all-parallel6-resilient-20260729-135044`: 89/89 complete, 53 pass / 36 fail, 5 errors, mean `0.5955`.
- Provider-fix replacement slice `kai-providerfix-zz-gpt55-llm17-20260729-232704`: 17/17 complete, 11 pass / 5 scored fail / 1 timeout, mean `0.6471` on that slice; no remaining provider-start parameter errors.
- Estimated single-run score after replacing broken provider-start cases: about `64/89 = 0.7191`; this is not an official clean full-run score.
- Normal-fail pass@n slice `kai-passn-zz-gpt55-normalfail15-20260729-233135`: 15/15 complete, 4/15 recovered on second attempt; keep this separate from pass@1 harness scoring.

## Current patch summary

- `experiments/evolved_harness/systemprompt.md`: adds verifier-command normalization so agents do not treat silent `python test_outputs.py` success as acceptance when `pytest`/evaluator-style runners are required; adds finalize-best-candidate guidance for long search/training loops.
- `scripts/build_templates.py`: relaxes E2B API-client construction to use the current `ConnectionConfig` directly.
- `experiments/evolved_harness/code_agent.gpt55-openai-chat.yaml`: documents the provider-compatible OpenAI-chat config that drops unsupported sampling params and uses `max_completion_tokens`.

## Next target knob

Primary next target: timeout/finalization guard for training/search tasks, starting with `train-fasttext`. Hypothesis: if the agent must persist the best raw-validation candidate before starting long searches and must publish it near the time limit, `AgentTimeoutError` turns into a scored attempt instead of a lost run.
