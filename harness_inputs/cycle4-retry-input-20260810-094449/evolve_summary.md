# Cycle 4 Retry Input — Reward-only Outer Analysis

## Safe evidence

- The stopped cycle-4 snapshot had 24 non-pass tasks among 83 completed tasks.
- Fourteen of the 24 had passed in cycle 3, so the result contains substantial rollout regression rather than only stable hard tasks.
- Fourteen of the 24 traces had no normal final response; ten of those were cycle-3 pass regressions.
- Repeated safe patterns were: a viable candidate not promoted to the required path, optional exploration continuing until task interruption, final state mutated after earlier validation, unavailable dependencies treated as syntax-only evidence, source/provenance substitution, and quantitative results with almost no safety margin.

No final answers, expected/reference answers, hidden test names or paths, verifier messages/traces/payloads, verifier stdout, or raw result/CTRF content were used.

## Original-code compatibility review

- NexAU normally stops when a model response contains no tool calls. A generic final gate would turn a valid final response into another iteration, so `enable_final_gate` remains disabled.
- The original shell tool promises Bash, but the underlying E2B command runner delegates shell selection and can execute with `/bin/sh`. Explicit `bash -c` restores the documented contract.
- The original return dictionary and tool schema are retained. Only the command passed to the backend is normalized.

## Minimal retry changes

1. Explicit Bash for foreground and background shell execution.
2. Real wall-clock delivery checkpoints at 15, 30, and 45 minutes. They do not fire during ordinary short tasks; late checkpoints prioritize the required artifact and exact final interface over optional branches.
3. Artifact-first wording at existing 10/20/40-shell progress gates.
4. A one-time continuation only when the agent has attempted shell work but every shell action failed. Normal successful final responses are untouched.
5. Advisory scope warning after destructive repository/ref/file-state operations; commands are not blocked or rewritten.

## Intentionally unchanged

- `systemprompt.md` is byte-identical to outer-cycle-3 and remains 500 words.
- `LongTermMEMORY.md`, tool description/schema, model settings, context size, and output formats are unchanged.
- Required builds, emulators, training jobs, explicit timeouts, and background execution remain available.
