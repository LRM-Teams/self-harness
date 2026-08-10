# Cycle 4 Retry2 Input — Manual Reward-only Repair

## Safe evidence

The stopped retry completed 26 tasks with 19 passes and 7 reward-zero outcomes. Five of the seven zero-reward cleaned traces lacked a normal final response. Direct agent-side evidence showed: a required background installation started without follow-up; a browser-verified candidate never reached the requested final path; an authoritative dataset download was replaced by synthetic data; a compiled GPT-2 implementation produced visibly invalid repetitive output; a recovery task located only a partial signature and never wrote its output; and a PyTorch implementation received only syntax/AST checks because the runtime was absent. One Git/web-server task passed a local root-only check but did not prove the actual login/persistence boundary.

No final answers, expected/reference answers, hidden test names or paths, verifier messages/traces/payloads, verifier stdout, or raw result/CTRF content were used.

## Repairs

1. Shell output is capped at 20k characters and retains a bounded head/tail view. Complete stdout and stderr remain available at the existing returned paths.
2. Old tool results compact at 55% of the 200k context budget while the six newest iterations remain intact.
3. Delivery checkpoints move from 15/30/45 minutes to 5/10/15 minutes. A behavioral success on a temporary candidate immediately names untouched explicit task paths.
4. Normal final responses are still not generically gated. A one-time continuation is used only for an untouched explicit path, a pending required background install/build/download, or syntax-only evidence after runtime failure.
5. Download timeouts now recommend resumable authoritative transfers and quiet progress logs. Synthetic/fake substitution after a failed download receives an explicit provenance stop warning.

## Benchmark integrity

No task-specific package, dataset, model, or answer is preloaded. Baking CIFAR-10, PyTorch, R, or other task-specific dependencies into selected E2B templates would change Terminal-Bench 2.0 conditions and confound comparison with earlier cycles. The prepared harness improves download continuation and context handling without changing task inputs.

This directory is a prepared retry2 input, not an outer-cycle-4 evolution result, and it has not been launched.
