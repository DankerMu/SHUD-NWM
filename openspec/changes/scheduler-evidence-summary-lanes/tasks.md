# Tasks — scheduler-evidence-summary-lanes (#3170)

## Risk packs

| Pack | Selected | Reason / mapping |
|---|---|---|
| Resource limits / large input / discovery | yes | hard 5 MB bound and fail-closed floor: E2, E3 fallback + byte-identity tests |
| Schema / columns / units / field names | yes | `model_run_evidence` row projection and `limit.lane_bytes` shape: E3 readiness-errors equality, lane_bytes measure pin |
| Legacy compatibility / examples | yes | tiers 1–4a byte-identical: E3 golden test |
| Error handling / rollback / partial outputs | yes | `SchedulerEvidenceWriteError` and the terminal floor unchanged: E3 fallback tests |
| Operator alerting lanes / observer-observed predicate parity | yes | stall probe `classify_pass` and `pass_limit_blocked`, `list-operator-actions` evaluating: E3 probe and decidability tests; E6 live |
| Run manifest / QC provenance | yes | readiness model-run validation: E3 `_scheduler_evidence_errors` equality |
| Documentation / migration notes | yes | task 1.6 |
| Public API / CLI / script entry | no | the CLI prints the written status and its code does not change; covered via `SchedulerPassResult.status` in E3 |
| Config / project setup | no | no config key changes; `MAX_EVIDENCE_BYTES` unchanged (non-goal) |
| File IO / path safety / overwrite | no | the atomic write path does not change |
| Concurrency / shared state / ordering | no | single-writer serialization, no new shared state |
| Auth / permissions / secrets | no | no surface |
| Release / packaging / dependency compatibility | no | no dependency change |
| Slurm production lifecycle / mock-vs-real parity | no | submission is unchanged; only its evidence is shaped |
| Others (geospatial, hydro-met, SHUD numerical, PostGIS, providers, display identity) | no | not touched |

- [x] 1.1 Triage: reconstruct the 132-candidate pass as design.md describes, rank lanes by indent-2 bytes after tiers 3–4,
  and record the table plus the script path in design.md.
- [x] 1.2 Tier 4b: compact retry of the candidate-list summary.
- [ ] 1.3 Tier 5: lane projection per design.md, starting with `model_run_evidence`. **Descoped from this PR**: the
  task 1.1 triage did not reproduce the overflow and real node-22 rows match the reconstructed shape (design.md,
  "Triage result"). Deferred to the E6 receipt; the reader analysis stays in design.md, "Deferred: tier 5 lane
  projection".
- [x] 1.4 Fallback `limit.lane_bytes` attribution, bounded, added only to an already fitted product that still carries
  `limit.pre_limit_status` (never at the reason-only floor).
- [x] 1.5 Tests per E3 (tiers 4b and `lane_bytes`): red on the old code for new behaviour, plus the golden
  byte-identity for tiers 1–4a. Route the new test file in the selector, including from the scheduler runtime, the
  readiness reader, the stall probe and the operator-action listing.
- [x] 1.6 Update the docs that describe the tiers, if any: `docs/runbooks/production-ops/stuck-detection.md` and
  `docs/runbooks/node22-*` mentions of the size fallback.
