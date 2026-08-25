# Tasks

## 1. Red first

- [ ] 1.1 Add a regression test reproducing the #1816 shape: a strict-warm-start
      terminal reconcile whose completed stage evidence belongs to a different
      `model_id` on the same `(basin_id, cycle_time, source_id)`, and no forcing
      package under the candidate's own `model_id`. Assert the current code emits
      `restart_stage: "forecast"` with no `forcing_provenance` — i.e. the test
      fails against `master`.
- [ ] 1.2 Add a regression test for the recorded-reference tier: a candidate
      whose inherited state records a `forcing_package_uri` under a foreign
      `model_id` that EXISTS on disk. Assert the witness must not accept it.

## 2. Consult the witness at the strict-warm-start emitting points

- [ ] 2.1 In `services/orchestrator/scheduler_candidates.py`, consult
      `_missing_upstream_forecast_artifact_evidence` for every strict-warm-start
      decision that carries `restart_stage: "forecast"`
      (`_strict_warm_start_terminal_retry_evidence`,
      `_strict_warm_start_run_manifest_retry_evidence`, and the run-manifest
      mismatch leg at `:2485`), following the pattern already used at the six
      emitting return points in `scheduler_state_decision.py`.
- [ ] 2.2 Record the provenance annotation through the same channel
      (`_record_forcing_provenance`) so it appears in pass evidence whether or
      not the decision blocked.
- [ ] 2.3 Return the blocker decision instead of the retry when the witness
      reports the package absent, preserving the witness's own reason and error
      code (`missing_forcing_package_uri` / `forcing_version_row_absent`).

## 3. Bind the recorded reference to the candidate's identity

- [ ] 3.1 In `services/orchestrator/scheduler_state_failure.py`, admit a
      recorded `forcing_package_uri` as this candidate's witness only when its
      key's trailing segments are this candidate's own
      `<basin_version_id>/<model_id>`. Compare on the recorded reference's own
      trailing segments; do NOT prefix-normalise it first.
- [ ] 3.2 On rejection, fall through to `_forcing_sidecar_provenance` exactly as
      an absent reference does, and name the rejection in `forcing_provenance`.
- [ ] 3.3 Leave every existing containment untouched: withheld placeholder
      recovery path (#1203 round-1 C1), probe-error "cannot determine" routing to
      `forcing_version_row_absent` (#1203 round-2 V5-C2), no probe fault escaping
      the pass (#1365 round-1), single manifest-key derivation through
      `_package_manifest_probe_uri`.

## 4. Green invariants

- [ ] 4.1 A strict-warm-start restart whose own forcing package exists emits the
      same decision and `restart_stage` as before.
- [ ] 4.2 A recorded reference naming the candidate's own model is probed
      unchanged.
- [ ] 4.3 Existing `strict-warm-start` and `production-scheduler-orchestration`
      tests still pass.

## 5. Evidence Floor

- [ ] 5.1 `uv run pytest -q tests/test_production_scheduler.py`
- [ ] 5.2 `uv run pytest -q tests/test_orchestration_chain.py`
- [ ] 5.3 `uv run ruff check .`
- [ ] 5.4 `openspec validate enforce-per-model-forcing-completeness --strict --no-interactive`
- [ ] 5.5 node-22 live receipt: the fix is scheduler decision logic, so it is
      verified on node-22 per the profile's verification matrix. Run one bounded
      scheduler pass from a throwaway clone (NEVER move
      `/scratch/frd_muziyao/NWM` onto a feature branch) against a fixture whose
      registry names a `model_id` with no forcing package for a completed cycle,
      and capture the pass evidence showing the candidate `blocked` with a named
      reason and a `forcing_provenance` annotation, with no forecast submission.

## 6. Write-back

- [ ] 6.1 Update issue #1826 with the measured scope correction (new-basin
      onboarding does NOT reproduce; re-identification does), the pass-date
      correction (the failing pass ran 2026-08-24T06:36:57Z, not 08-23), and the
      now-filled `Verification:` field.
- [ ] 6.2 File the two reported-not-fixed seams
      (`chain_forecast_cycle.py:490` unscoped cycle-wide job fallback,
      `:523` model-blind `job_matches_stage`) as a tracked issue.
- [ ] 6.3 File the "auto re-enter the forcing stage instead of blocking"
      follow-up as a tracked issue, referencing this change's Decision section.
