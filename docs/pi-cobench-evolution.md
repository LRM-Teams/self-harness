# Pi + DeepSeek evolution for CO-Bench

This specialization runs the generic three-lane candidate graph against the official CO-Bench development evaluator:

- 64 formal development evaluations;
- three independent candidates per full generation (21 full generations plus one final slot);
- one-CPU, 10-second solver limit;
- early random-immigrant lane, later crossover and focused refinement;
- feasibility/timeout repair when invalidity rises;
- post-evaluation debugging and periodic mechanism-level communication;
- one final test evaluation only after the development budget is exhausted.

The adapter constructs a development-only data view during search, evaluates only the instances selected by the task's public `get_dev()` split, and returns only `dev_score` and `dev_feedback`. Final test output is written only under `final/` after selection.

## Setup

Download the official code and dataset, then build the isolated evaluator image:

```bash
git clone https://github.com/sunnweiwei/CO-Bench.git /path/to/CO-Bench
huggingface-cli download CO-Bench/CO-Bench \
  --repo-type dataset --local-dir /path/to/co-bench-data
docker build -t self-harness-cobench:latest -f docker/cobench/Dockerfile .
```

Configure one task and run:

```bash
export COBENCH_REPO=/path/to/CO-Bench
export COBENCH_DATA_DIR=/path/to/co-bench-data
export COBENCH_TASK='Aircraft landing'
export COBENCH_OUTPUT_DIR=/path/to/runs/aircraft-landing-seed-1
export LENOVO_DEEPSEEK_V4_FLASH_API_KEY=...

uv run pi-cobench-evolution \
  --config configs/experiments/exp-pi-cobench.yaml
```

The Docker evaluator runs with networking disabled, one CPU, a read-only root filesystem, read-only code/data/candidate mounts, and a writable temporary filesystem. Set `execution_backend: local` only for trusted development and adapter tests.

Before execution, the adapter also rejects missing/async `solve` functions and common filesystem, process, dynamic-code, and network access primitives. Rejected candidates still consume one of the 64 search slots but are not executed.

Every task must use a separate `output_dir`. A persisted run manifest rejects accidental resume with a different task or evaluator configuration. The archive is resumable and never overwrites evaluated candidates; an existing final-test result is reused instead of querying the test set again.
