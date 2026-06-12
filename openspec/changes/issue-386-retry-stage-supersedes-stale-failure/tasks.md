## 1. Stage Repair Semantics

- [x] 1.1 Identify linked successful manual retry jobs for a failed logical
  cycle stage using durable retry provenance and stage/run/cycle identity.
  Phase 6 round 2 extends source-cycle provenance transitively across bounded
  manual retry chains so a latest successful retry repairs failed ancestors in
  the same source/cycle/run/stage/job_type chain.
- [x] 1.2 Treat older failed source-cycle stage evidence as repaired/superseded
  when a linked retry succeeded and source-cycle readiness is restored. Repaired
  failed rows remain in evidence but are excluded from active scheduler
  blocker/failure decisions.
- [x] 1.3 Keep unrelated successful jobs from masking unrepaired failures.
- [x] 1.4 Preserve existing partial array retry supersession semantics.

## 2. Evidence Contract

- [x] 2.1 Add or populate stable evidence fields that identify original failed
  job, repairing retry job, repaired stage, and repair status.
- [x] 2.2 Ensure `stage_statuses` / `stage_evidence` no longer present repaired
  failures as active blockers.
- [x] 2.3 Ensure unrepaired failures still emit stable failed candidate evidence.
- [x] 2.4 Keep evidence reads bounded by existing job/event limits and mark
  truncation when applicable. Phase 6 round 2 treats truncated source-cycle
  repair windows with matching ready manifests as inconclusive rather than
  complete negative proof, and does not promote unresolved rows from that
  truncated repair window to active source-cycle blockers. Phase 6 round 3
  preserves that inconclusive evidence on scheduler proceed paths while
  suppressing retry/manual-retry decisions from the listed unresolved historical
  rows.
- [x] 2.5 Require repaired source-cycle evidence to agree with
  `forecast_cycle.manifest_uri`; a missing or mismatched manifest URI must not
  turn a stale failure into repaired evidence. Phase 6 extends this to prefixed
  S3 object-store URIs and rejects unsupported `https://` / `file://` schemes,
  unsafe `..` segments, and source/cycle/filename mismatches; wrong bucket/prefix
  validation remains limited to paths where repository object-store prefix
  context is available.
- [x] 2.6 Preserve monitoring/pipeline API compatibility: existing response
  envelopes and field names remain stable, while any new repair metadata is
  additive and bounded.

## 3. Risk-Pack Evidence Map

- [x] 3.1 Public API / CLI / script entry: run focused monitoring/pipeline API or
  contract tests proving response envelopes remain stable when repaired stage
  metadata is present. Phase 6 evidence includes a scheduler pass/operator path
  assertion that repaired metadata is carried in candidate/model-run/submission
  evidence, plus a monitoring API contract test proving `/pipeline/status` and
  `/pipeline/stages` keep stable envelopes and do not report the repaired stale
  source-cycle failure as active.
- [x] 3.2 Schema / field names and provenance: assert repair evidence contains
  original failed job id, repairing retry job id, repaired stage, repair status,
  and manifest binding fields when available.
- [x] 3.3 Concurrency / shared state / ordering: cover a stale manual retry
  marker or older non-succeeded retry after the original failure; it must not
  supersede the active failure. Phase 6 adds mixed ordering coverage where an
  older repaired source-cycle failure remains annotated while a later separate
  unrepaired source-cycle failure stays active. Phase 6 round 2 adds equal truth
  timestamp coverage using terminal time, retry count, created_at, and job id
  as deterministic tie-breakers.
- [x] 3.4 Resource limits / discovery: cover bounded/truncated job or event
  history without unbounded scans. Phase 6 round 2 covers truncated source-cycle
  repair proof as explicit inconclusive bounded evidence. Phase 6 round 3 adds
  scheduler no-event and manual-retry-event regressions proving unresolved
  truncated rows stay historical decision evidence.
- [x] 3.5 Legacy compatibility: rerun existing array task retry supersession and
  non-source retry tests without changing their behavior.
- [x] 3.6 Error handling / partial outputs: cover unrepaired failure and
  manifest mismatch cases returning stable failed/retry evidence.
- [x] 3.7 Documentation / migration notes: update operator runbook with diagnosis
  and safe remediation guidance.
- [x] 3.8 Hydro-met time series / forcing windows: bind repair to the same
  source/cycle identity and forecast-cycle status.
- [x] 3.9 Slurm lifecycle / mock-vs-real parity: bind retry repair to successful
  retry job status and Slurm/pipeline job identity, not just a successful sibling
  job.
- [x] 3.10 External provider snapshot reproducibility: ensure IFS/GFS cycle
  identity and manifest URI are preserved in evidence.
- [x] 3.11 Run manifest / QC provenance: cover matching `raw_complete`
  `forecast_cycle.manifest_uri` as a positive case and mismatched/missing
  manifest URI as a negative case.

## 4. Tests and Documentation

- [x] 4.1 Add regression for `download_source_cycle` permanently_failed followed
  by successful linked manual retry and `forecast_cycle.status=raw_complete`:
  scheduler/candidate evidence proceeds and marks old failure repaired, with
  `manifest_uri` matching the repaired source/cycle. Phase 6 includes prefixed
  S3 manifest URI coverage for
  `s3://nhms-prod/qhh/raw/gfs/2026050100/manifest.json`. Phase 6 round 2 adds
  scheduler coverage where repaired source-cycle failed rows plus the actual
  manual retry event do not trigger `manual_retry_requested`. Phase 6 round 3
  adds scheduler coverage for unrepaired shared source-cycle blockers that lack
  candidate-scoped rows.
- [x] 4.2 Add negative regression where a successful unrelated job does not
  supersede the failed stage.
- [x] 4.3 Add negative regression where a stale manual retry marker or
  non-succeeded retry is newer than the failure but must not repair it. Phase 6
  round 2 adds positive multihop retry-chain coverage proving original and
  intermediate failed retry ancestors are historical after the latest successful
  retry.
- [x] 4.4 Add negative regression where `forecast_cycle.manifest_uri` is missing
  or mismatched, so the old failure remains active. Phase 6 adds unsupported
  scheme (`https://`, `file://`), unsafe path, wrong source/cycle, and wrong
  filename regressions.
- [x] 4.5 Re-run existing array task retry supersession coverage to prove no
  regression.
- [x] 4.6 Re-run focused non-source/manual retry compatibility coverage if helper
  code changes touch retry identity.
- [x] 4.7 Update operator runbook with stale stage evidence diagnosis and safe
  remediation guidance.

## 5. Verification

- [x] 5.1 Run focused scheduler/orchestration tests for candidate state and retry
  supersession.
- [x] 5.2 Run focused monitoring/API tests if evidence shape changes public
  surfaces.
- [x] 5.3 Run `uv run --no-sync ruff check .` or focused ruff on touched files.
- [x] 5.4 Run
  `openspec validate issue-386-retry-stage-supersedes-stale-failure --strict --no-interactive`.
