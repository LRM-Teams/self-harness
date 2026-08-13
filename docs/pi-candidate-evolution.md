# Generic Pi candidate evolution

This runner evolves benchmark candidates without changing the frozen base model. It uses three independent lanes per generation:

1. refine the best feasible candidate;
2. continue a high-quality structurally diverse candidate;
3. adaptively repair, cross over, restart, or refine according to stagnation, invalid rate, diversity, and remaining budget.

Every generated candidate occupies one formal evaluation slot. Candidates, parent edges, cross-branch reference edges, evaluator results, debug reports, Pi traces, and retrospective memory are persisted under the configured `output_dir`, so interrupted runs can be inspected and completed candidates remain auditable.

The framework is evaluator-agnostic. Implement `validate(candidate_dir)` and `evaluate(candidate_dir)` using the `EvaluationAdapter` protocol, or use `ShellEvaluationAdapter` with commands that emit JSON. Adapter scores must be normalized so higher is better.

Run:

```bash
uv run pi-candidate-evolution \
  --config configs/experiments/exp-pi-candidate-evolution-example.yaml
```

Independent candidates do not see same-generation work. Communication happens only after evaluation through compact result cards and retrospective memory, reducing premature branch collapse.
