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

## 5. Evidence Mapping

- [ ] 5.1 Schema/type drift: tests cover every status and execution_mode, invalid status/mode rejection, required artifact/residual-risk/removal-criteria fields, final readiness false when any required live blocker exists, and out-of-scope exclusion handling.
- [ ] 5.2 Live opt-in safety: tests prove default/fast-CI command does not execute live IdP, alert sink, Slurm, object store, weather/source, rollback, or real-national-data operations.
- [ ] 5.3 Secret/path redaction: tests cover auth provider metadata, alert sink URLs, dependency receipts, live proof payloads, artifact paths, tokens, credentials, query strings, and oversized/deep proof payloads.
- [ ] 5.4 Existing lane compatibility: tests consume representative M10/M16/M17/M18-style evidence and keep existing production ops/object-store/slurm/met/e2e/scale validation tests green.
- [ ] 5.5 Release blocker summary: tests cover deterministic pass plus missing live blockers, deterministic failure, malformed live receipt, explicit CLDAS exclusion, incomplete national data exclusion, and all-live-proof accepted summary without overclaiming.
- [ ] 5.6 Status/mode allowed-combination tests: each valid combination is accepted, each forbidden combination is rejected, and required-live proof failure is represented as `release_blocked` + `live_proof` + `live_proof_accepted=false`.
- [ ] 5.7 Final readiness false-positive tests: deterministic pass with any missing/incomplete/failed required live proof keeps `final_production_readiness_claimed=false` and emits blocker ids/removal criteria.
- [ ] 5.8 Fast-CI no-live side-effect tests: monkeypatch or sentinel live clients prove default readiness does not call live auth, alert, rollback, Slurm, object-store, weather/source, or real-national-data operations.
- [ ] 5.9 Redaction/bounds tests: stdout and JSON artifacts do not contain raw tokens, credentials, URL userinfo/query strings, local paths, signed URLs, deep payload leaves, or oversized receipt bodies.
