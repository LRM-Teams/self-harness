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
- Candidate/guess non-code answer files are not deliverables. In visual/geometric extraction, G-code text recovery, Raman/quantitative fitting, sequence editing, and similar tasks, do not finalize until a different method confirms the character/shape/parameter choice. Source-code deliverables may be developed incrementally at their required paths.
- For Git/SSH/webserver/QEMU/service tasks, final evidence must be client-facing after the last cleanup/reset: exact host alias/remote path, auth mode, URL, branch mapping, port, and hook behavior. On-disk existence checks are not enough.
- Before opening WAL/database or other stateful forensic evidence, byte-copy the primary file plus journals and sidecars; ordinary SQLite access may replay or remove the evidence needed for recovery.
- Treat exact method, message, field, key, and filename spelling as contract data copied from the request. Validate it with an independently written client/parser, not code generated from the implementation under test.
- Keep candidates, placeholders, and known below-threshold outputs under `/tmp`. Quantitative deliverables require an observed metric above the stated threshold with practical safety margin before promotion.
- Source-code deliverables may legitimately contain words like `dummy` or `fake` in variables/comments/tests; do not let placeholder hygiene block writing required code paths such as `/app/*.py`. Apply hard placeholder blocking mainly to final data/answer artifacts.
- Once an exact public invocation plus semantic assertions pass after the last mutation, finalize promptly. Optional edge-case broadening after a passing check caused timeout risk and can regress a working artifact.
- Biosequence and gBlock outputs must be concrete DNA (`A/C/G/T`) unless ambiguity is requested. Resolve PDB modified residues to concrete precursor amino acids/codons instead of encoding `N`.
- Golden Gate designs should use biological part boundaries and high-fidelity non-palindromic overhangs, verified by simulating Type-IIS cuts on actual PCR products; string equality alone cannot justify junction shifting through coincidental overlap.
- Distributed tensor-parallel layers need reference comparisons for forward and input/weight/bias gradients in both real process groups and non-initialized monkeypatched rank/world-size contexts, including full/local row inputs and non-divisible shapes.
- Raman fits must lock a justified raw-axis transformation before peak selection. Graphene G/2D results should land near physical Raman shifts (~1580/~2670 cm^-1), using `1e7/raw_x` when the raw axis is reciprocal wavelength encoded.
- Final format and exact-interface gates should be evidence-driven: a successful parser/compiler/client check is valid only for the latest artifact/state revision, and a later mutation makes it stale. Do not force a redundant final model turn when fresh independent evidence already exists.
