# Enforce forcing completeness per (cycle, model_id), not per cycle

## Why

Forcing artifacts are stored **per model**:

```
<object-store>/forcing/<source>/<cycle>/<basin_version_id>/<model_id>/
```

`model_id` (`dg_<32hex>`) is derived from package content, so a republish that
changes package content mints a new identity. The scheduler's judgement of
whether a cycle's forcing stage is done is **not** per model. Two seams carry
that coarser grain:

- `services/orchestrator/chain_forecast_cycle.py:490` — when
  `_candidate_scoped_cycle_execution` is false the job set fed to the stage loop
  is `self._query_pipeline_jobs_by_cycle(context.cycle_id)`: every job for the
  whole cycle, all models.
- `services/orchestrator/chain_forecast_cycle.py:523` — `job_matches_stage`
  compares `stage` / `job_type` only. It never compares `model_id`. A terminal
  `forcing` job belonging to a *different* model therefore satisfies
  `find_existing_stage_job`, `_run_cycle_chain` takes the `_resume_cycle_stage`
  branch instead of resubmitting, and the candidate proceeds to `forecast` with
  no forcing package of its own.

The forecast stage then dies in 1–2 seconds at
`workers/shud_runtime/runtime.py:2034`:

```
ARTIFACT_NOT_FOUND: Object storage artifact not found:
s3://nhms/forcing/gfs/2026082300/basins_heihe_vbasins/dg_f6175cdb0f3825bec4807c386b5cbf38/
```

A per-model existence witness already exists and is hardened
(`services/orchestrator/scheduler_state_failure.py`, #1203 / #1365:
`_forcing_sidecar_provenance`, `_package_manifest_probe_uri`). It is unreachable
on a first attempt, because `_missing_upstream_forecast_artifact_evidence`
returns early when there is no `planned_retry`
(`scheduler_state_failure.py:602-603`). Nothing checks a candidate's own forcing
package before its forecast is submitted.

## Scope correction against the issue text

Issue #1826 asserts the defect "对'新上线流域中途加入'同样成立" — that a newly
onboarded basin joining mid-history hits the same wall. **Measured on node-22
2026-08-25 and refuted.** Seven new Yellow-River sub-basins were registered at
14:58:32 CST; by 15:19:57 CST the scheduler had produced forcing for all seven
under cycle `2026081012` for both `gfs` and `ifs` on the scheduler's own object
store (`/scratch/frd_muziyao/nhms-prod/object-store`, per
`infra/env/compute.scheduler-dbfree.env:13`), each holding
`forcing_package.json`, `forcing.tsd.forc`, `forcing_version_record.json` and
`shud/`. A brand-new `model_id` on a cycle long since judged complete for the
other 24 basins was **not** skipped.

The reproducible trigger is therefore narrower than the issue states:
**re-identification** — same `basin_version_id`, new content-derived `model_id`,
on a cycle whose chain already holds a terminal `forcing` job. That is the
#1816 republish shape, and it is what this change must make impossible.

## What Changes

- **ADDED** requirement: a strict-warm-start decision carrying
  `restart_stage: "forecast"` SHALL consult the per-model forcing witness for the
  candidate's own `(source, cycle, basin_version_id, model_id)` before it is
  emitted, and SHALL block with the witness's named reason when the package is
  absent.
- **ADDED** requirement: a `forcing_package_uri` recorded in inherited state
  SHALL witness only the candidate whose `<basin_version_id>/<model_id>` its key
  names; any other reference is treated as absent and the identity-derived tier
  runs instead.
- **MODIFIED**: `services/orchestrator/scheduler_candidates.py` — the
  strict-warm-start emitting points consult
  `_missing_upstream_forecast_artifact_evidence`, following the pattern
  `services/orchestrator/scheduler_state_decision.py` already applies at its six
  emitting return points.
- **MODIFIED**: `services/orchestrator/scheduler_state_failure.py` — the
  recorded-reference tier of `_missing_upstream_forecast_artifact_evidence` gains
  the identity-binding check, and names the rejection in `forcing_provenance`.
- **ADDED**: regression tests reproducing the #1816 incident shape, plus the
  green invariants (own-uri candidate unchanged, placeholder and probe-error
  containments unchanged).

## Non-goals

- Decoupling package identity from calibration content (#1813). Identity will
  keep changing; this change makes the change survivable, not rarer.
- Replacing `scripts/node22_backfill_forcing_for_model_ids.py` (#1825). That
  tool repairs cycles that already went wrong; this change stops new ones. Both
  stay.
- Re-issuing any forecast produced before a republish. Issued history is not
  retroactively corrected.
- Automatically re-entering the forcing stage instead of blocking. Filed as a
  follow-up; see `design.md` for why it is a separate decision.
- The unscoped cycle-wide job fallback at `chain_forecast_cycle.py:490` and the
  model-blind `job_matches_stage` at `:523`. Reported, not fixed.
- Restarting runs that already failed with `ARTIFACT_NOT_FOUND`. Those are
  classified permanent and are restarted only through the policy-gated
  manual-retry marker (#1825), unchanged here.
