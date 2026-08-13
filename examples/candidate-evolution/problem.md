# Candidate contract

Implement the benchmark-specific solver in `solution.py`. Replace this example with the public problem description, exact function signature, return schema, constraints, and runtime limit.

The configured evaluator must print one JSON object:

```json
{
  "score": 0.0,
  "feasible": true,
  "feedback": "development-set feedback",
  "runtime_seconds": 0.0,
  "metrics": {}
}
```

Scores exposed to the generic evolution engine must be normalized so that higher is always better.
