## 1. Evidence Schema

- [ ] 1.1 Define the readiness status/execution-mode truth table, dependency references, artifact paths, residual risk, exclusions, and blocker removal criteria.
- [ ] 1.2 Add schema/unit tests for passed, failed, blocked, not_executed, release_blocked, and out-of-scope exclusions.

## 2. Deterministic Readiness Lane

- [ ] 2.1 Add deterministic readiness command/report that consumes current demo/Basins/production-like evidence and M17/M18/M16 outputs when available.
- [ ] 2.2 Ensure missing live IdP, alert sink, Slurm, object store, live weather credentials, and real national data are recorded truthfully without failing deterministic checks by default.
- [ ] 2.3 Add fixture tests for deterministic pass, deterministic failure, missing live dependency, and explicit CLDAS/national-data exclusions.

## 3. Opt-in Live Proof Lane

- [ ] 3.1 Add opt-in live proof inputs for auth provider, alert sink, rollback drills, Slurm/object-store/source dependency receipts, and MVT/performance evidence.
- [ ] 3.2 Redact live credentials and record provider/sink/drill metadata, result, artifact paths, and residual risks.
- [ ] 3.3 Add tests proving live mode is never executed accidentally in fast CI.

## 4. Reporting and Docs

- [ ] 4.1 Generate release blocker summary with blocker id, surface, status, residual risk, removal criteria, and artifact links.
- [ ] 4.2 Update validation docs and `progress.md` with deterministic/live readiness interpretation, CLDAS exclusion, and real-national-data exclusion.
- [ ] 4.3 Run OpenSpec strict validation, targeted readiness tests, `uv run ruff check .`, and relevant production-closure regression tests.
