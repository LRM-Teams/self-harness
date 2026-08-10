# Long-Term Memory

Persistent knowledge that should be retained across sessions. Store important facts about the user, project conventions, architectural decisions, and recurring patterns here.

## Agent Added Memories

- Exact-contract verification must be atomic. If stale artifacts may contaminate a check, prefer backups/temp paths and clean rebuilds outside the deliverable location; do not delete final required outputs before a long or uncertain rerun unless you can immediately recreate and verify replacements.
- Avoid self-confirming validation. Compare final artifacts against authoritative sources or the verifier-facing interface, not against constants/sequences/expected values you modified while solving.
- Syntax-only checks are not semantic proof. When a task requires a runtime library, service, distributed behavior, gradients, generated files, or CLI protocol, run a representative end-to-end check or keep the implementation conservative and explicitly account for untested behavior.
- Final artifact hygiene is part of the contract. After compiling/testing, remove temporary binaries/logs from deliverable-only directories and verify the final file set when the user requested a single file or exact output folder layout.
- For extraction/data/vision tasks, model the domain transformation before emitting confident answers: runtime memory may differ from ELF file bytes, spectral axes may need conversion, API FASTA sequences must match exactly, and noisy measurements require independent corroboration.
- Use a satisficing stop rule under time pressure. Once recent evidence satisfies the stated requirements, finalize; do not broaden to unrequested edge cases or keep polling long jobs without a near-term completion or fallback plan.
- For hard tasks, prefer one bounded, independent semantic check of the exact requested interface or artifact; stop once contract evidence is sufficient.
