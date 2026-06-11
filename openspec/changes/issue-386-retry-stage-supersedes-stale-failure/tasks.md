## 1. Stage Repair Semantics

- [x] 1.1 Identify linked successful manual retry jobs for a failed logical
  cycle stage using durable retry provenance and stage/run/cycle identity.
- [x] 1.2 Treat older failed source-cycle stage evidence as repaired/superseded
  when a linked retry succeeded and source-cycle readiness is restored.
- [x] 1.3 Keep unrelated successful jobs from masking unrepaired failures.
- [x] 1.4 Preserve existing partial array retry supersession semantics.

## 2. Evidence Contract

- [x] 2.1 Add or populate stable evidence fields that identify original failed
  job, repairing retry job, repaired stage, and repair status.
- [x] 2.2 Ensure `stage_statuses` / `stage_evidence` no longer present repaired
  failures as active blockers.
- [x] 2.3 Ensure unrepaired failures still emit stable failed candidate evidence.
- [x] 2.4 Keep evidence reads bounded by existing job/event limits and mark
  truncation when applicable.
- [x] 2.5 Require repaired source-cycle evidence to agree with
  `forecast_cycle.manifest_uri`; a missing or mismatched manifest URI must not
  turn a stale failure into repaired evidence.
- [x] 2.6 Preserve monitoring/pipeline API compatibility: existing response
  envelopes and field names remain stable, while any new repair metadata is
  additive and bounded.

## 3. Risk-Pack Evidence Map

- [x] 3.1 Public API / CLI / script entry: run focused monitoring/pipeline API or
  contract tests proving response envelopes remain stable when repaired stage
  metadata is present.
- [x] 3.2 Schema / field names and provenance: assert repair evidence contains
  original failed job id, repairing retry job id, repaired stage, repair status,
  and manifest binding fields when available.
- [x] 3.3 Concurrency / shared state / ordering: cover a stale manual retry
  marker or older non-succeeded retry after the original failure; it must not
  supersede the active failure.
- [x] 3.4 Resource limits / discovery: cover bounded/truncated job or event
  history without unbounded scans.
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
  `manifest_uri` matching the repaired source/cycle.
- [x] 4.2 Add negative regression where a successful unrelated job does not
  supersede the failed stage.
- [x] 4.3 Add negative regression where a stale manual retry marker or
  non-succeeded retry is newer than the failure but must not repair it.
- [x] 4.4 Add negative regression where `forecast_cycle.manifest_uri` is missing
  or mismatched, so the old failure remains active.
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
