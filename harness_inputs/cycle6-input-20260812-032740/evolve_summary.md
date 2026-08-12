# Iteration 1 Evolution Summary

## Evidence reviewed

Primary analysis came from `runs/iteration_001/input/analysis/overview.md` and selected detail/trace files under the sanitized feedback directory. Most auto-generated detail reports initially said the minified traces were hard to inspect, but the staged `trace01.json` files were readable with line offsets.

Observed failure patterns:

1. **Candidate extraction/fit artifacts finalized as answers.**
   - `gcode-to-text` wrote `/app/out.txt` with `The quick brown fox jumps over the lazy dog` from a command explicitly described as writing a "candidate" after visual G-code rendering.
   - `raman-fitting` wrote `/app/results.json` from "candidate fitted Raman peak parameters" and validated only JSON schema/existence before finalizing.
   - Related risk class includes video/geometric extraction and sequence-editing tasks where plausible-looking outputs need independent confirmation.

2. **Service/Git tasks downgraded final evidence after cleanup.**
   - `configure-git-webserver` verified a local `/git/server` clone and `127.0.0.1` curl, then reset the repository/webroot; it did not prove the exact requested `user@server:/git/server` / `http://server:8080/...` interface after final cleanup.
   - `git-multibranch` had an end-to-end push/curl check, then cleaned test content and the last recovery turn rechecked only `/git/project` and the hook on disk.

3. **Model-time overengineering on small single-file tasks.**
   - `filter-js-from-html` exceeded the practical rollout budget largely through long model generations around a large first implementation and follow-up checks, not through slow shell execution.

## Changes made

### chg-1: Uncertain-output recovery gate

`middleware/execution_guard.py` now detects commands that write final-looking `/app` output artifacts while the command/content labels them as a candidate, guess, approximation, ambiguous/uncertain result, or visual/manual read. It appends an immediate warning and records the run. If the agent then tries to finalize, the middleware forces one recovery turn requiring independent confirmation from a different method before finalization.

The same principle was added to `systemprompt.md`, `tool_descriptions/run_shell_command.tool.yaml`, and `LongTermMEMORY.md`.

### chg-2: Scoped exact-interface final gate

`middleware/execution_guard.py` now stores the user request and, for service/Git/SSH/webserver/QEMU/host/port/deployment-hook tasks, injects a one-time final recovery turn. The recovery requires a post-cleanup exact client-facing check: requested host alias/remote path, URL, protocol, port, branch/path mapping, auth mode, and latency when specified. This is deliberately scoped rather than enabling the global final gate for all tasks.

Prompt/tool/memory guidance now reinforces that on-disk checks or local-file substitutes are insufficient for these tasks when the requested interface can be exercised.

### chg-3: Small-first single-file implementation guidance

`systemprompt.md` now explicitly tells the agent to create compact baseline implementations first for single-file parser/filter/converter tasks, then test and extend, rather than spending a long first generation on all possible edge cases.

## Predicted impact

Expected improvements:

- `gcode-to-text`, `raman-fitting`, `extract-moves-from-video`, `video-processing`, and possibly `dna-insert` should be less likely to finalize unsupported plausible outputs.
- `configure-git-webserver`, `git-multibranch`, and possibly `install-windows-3.11` should gather more evaluator-facing evidence before finalization.
- `filter-js-from-html` should be less likely to time out from first-turn overengineering.

Risks:

- Scoped final gate adds one extra turn to some previously passing service/QEMU tasks (`nginx-request-logging`, `qemu-startup`, `qemu-alpine-ssh`, `pypi-server`). It is scoped to avoid broad regressions.
- Uncertain-output gate may spend one extra turn on successful extraction tasks if the agent labels an artifact as a candidate. This is intended only when the agent itself indicates uncertainty.

## Validation

`inspect_workspace validate` passed: agent YAML parsed, Python files compiled, and `git diff --check` passed.

No `agent/nexau.txt` runtime log was present under the sanitized feedback directory; staged traces only contained `trace01.json` files.

## Cycle 5 regression-repair continuation

This copied harness was manually repaired after the cycle 5 run was stopped at the 80% monitoring line. It is not a new claim about the prior automatic evolution. The repair narrows the uncertain-output gate to commands that actually write artifacts, protects SQLite/WAL evidence before first access, blocks explicit placeholders at required final paths, adds exact-schema and numeric-threshold checks, and gives complex tasks more room before progress warnings.

The next evaluation is intentionally scoped to the nine pass-to-fail regressions and seventeen tasks that had not terminated. Its deduplicated stitched result is an infrastructure-adjusted continuation metric and must remain separate from the original cycle 5 score.

## Cycle 6 high-confidence repair

Cycle 6 starts from the continuation workspace at commit `24587cc` / tag `iteration_1`. The automatic evolution correctly identified the source-code placeholder false positive, the need to stop after a post-mutation exact semantic pass, concrete biosequence output, and fuller tensor-parallel validation. Before full evaluation, those ideas were narrowed to reduce regression risk:

- `dummy`/`fake` are exempt only for required source-code paths; strong placeholder markers remain blocked everywhere and soft placeholders remain blocked for final data artifacts.
- Raman, biosequence, distributed tensor-parallel, and Golden Gate guidance each has a task-scoped one-time final gate tied to observed pass/fail trace contrasts.
- The broad all-script hidden-like test requirement was removed; it did not deterministically fix the SAM failure and could consume extra time across unrelated passing scripts.
- No global segmentation/contour algorithm was inferred from the single MobileSAM demo failure.

The full cycle 6 score must be labeled infrastructure-adjusted because the E2B templates retain preloaded recovery dependencies.
