# Tasks and Evidence Floor — #1356

- [ ] 1. Reproduce deterministic duplicate submission at the retry-ID selection seam and retain complete cycle rows with identity/status/attempt/reconciliation fields.
- [x] 2. Record real guard-hole qualification (different retry keys, two ownerships and gateway calls), pre-existing provenance, and production possibility; mark test-only alternative inapplicable.
- [ ] 3. Make all three retry-ID callsites consume their parent-selection jobs snapshot; preserve explicit retry precedence, suffix semantics and operator-demotion behavior; migrate both direct test callers.
- [ ] 4. Add deterministic public-seam interleaving regression while retaining natural concurrency, original attempt/projection/error/cancellation assertions and unique active-master checks.
- [ ] 5. Run old-method assertion-red then restored-green with complete row evidence; run affected suites and disposable DB owners on node27 without skipped acceptance owners.
- [ ] 6. Run both sources in natural and deterministic modes for20 consecutive rounds under bounded node27 CPU pressure, all passing; run local Ruff and strict OpenSpec.
- [ ] 7. Complete independent review/finding verification/final review, CI, SHA-bound evidence/self-audit and merge/accountability; then resume blocked #2208.

Fixture expanded; risk packs, invariant matrix, mutation seam, baseline and rollback boundaries are in design.md. Required merge evidence is not satisfied by an occasional green rerun. Existing journal/CAS/gateway semantics are immutable; no production deployment or live Slurm claim. Parent owns all verification. Code changes only the two retry-selection modules and related tests; selector edge changes require an observed missing owner, not a new policy.
