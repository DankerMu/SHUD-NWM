# Tasks

## 1. Red first

- [x] 1.1 Add a regression test reproducing the #1816 shape: a strict-warm-start
      terminal reconcile whose completed stage evidence belongs to a different
      `model_id` on the same `(basin_id, cycle_time, source_id)`, and no forcing
      package under the candidate's own `model_id`. Assert the current code emits
      `restart_stage: "forecast"` with no `forcing_provenance` — i.e. the test
      fails against `master`.
- [x] 1.2 Add a regression test for the recorded-reference tier: a candidate
      whose inherited state records a `forcing_package_uri` under a foreign
      `model_id` that EXISTS on disk. Assert the witness must not accept it.

## 2. Consult the witness at the strict-warm-start emitting points

- [x] 2.1 In `services/orchestrator/scheduler_candidates.py`, consult
      `_missing_upstream_forecast_artifact_evidence` at the three
      **`build_candidates`** call sites that emit a strict-warm-start decision
      carrying `restart_stage: "forecast"` — around `:541-548` (run-manifest
      missing leg), `:570-574` (`_strict_warm_start_terminal_mismatch_decision`),
      and `:616-619` (`_upgrade_retry_for_strict_warm_start_manifest`). Do NOT
      edit the evidence builders `_strict_warm_start_terminal_retry_evidence`
      (`:2436`), `_strict_warm_start_run_manifest_retry_evidence` (`:2459`), or
      `_strict_warm_start_retry_run_manifest_evidence` (`:2479`): they are pure
      `Mapping -> Mapping` payload constructors with neither `candidate` nor the
      raw candidate `state` in scope. `build_candidates` owns all four guard
      arguments, which is the same shape `scheduler_state_decision.py` uses at
      its emitting return points.
- [x] 2.1a Do NOT widen `_upgrade_retry_for_strict_warm_start_manifest`'s 2-arg
      signature: five existing tests call it by name
      (`tests/test_production_scheduler.py:186, 225, 261, 294, 10963`). The
      consultation belongs AFTER it returns, at `:616-619` — the pre-upgrade
      decision may have been guard-checked at a different `restart_stage`, so the
      guard must run again on the rewritten decision regardless.
- [x] 2.1b Return the guard's blocker payload **verbatim** rather than re-merging
      it, so its self-tagged `classifier` / `artifact_guard.stable_classifier`
      still land in `_decision_is_stable_missing_forcing_blocker` (`:1574-1594`).
- [x] 2.2 Record the provenance annotation through the same channel
      (`_record_forcing_provenance`) so it appears in pass evidence whether or
      not the decision blocked.
- [x] 2.3 Return the blocker decision instead of the retry when the witness
      reports the package absent, preserving the witness's own reason and error
      code (`missing_forcing_package_uri` / `forcing_version_row_absent`).

## 3. Bind the recorded reference to the candidate's identity

- [x] 3.1 In `services/orchestrator/scheduler_state_failure.py`, admit a
      recorded `forcing_package_uri` as this candidate's witness only when its
      key's trailing segments are this candidate's own
      `<basin_version_id>/<model_id>`. Before comparing, remove exactly two
      trailing shapes and nothing else: (a) a trailing `/`, and (b) a final
      segment equal to `_FORCING_PACKAGE_MANIFEST_FILENAME`. Both shapes are
      documented as coexisting in production — see the `_needs_package_manifest_witness`
      docstring (`:1073-1100`, producer directory uri vs handoff-lane stripped
      copy) and `_sidecar_manifest_probe_key` (`:1030-1051`, manifest FILE key).
      A naive `endswith` without these removals rejects the candidate's OWN
      reference and blocks a healthy candidate. Do NOT prefix-normalise the
      recorded reference; that is the actual documented hazard. Fewer than two
      segments left after the removals means not bound.
- [x] 3.2 On rejection, fall through to `_forcing_sidecar_provenance` exactly as
      an absent reference does, and name the rejection in `forcing_provenance`.
- [x] 3.3 Leave every existing containment untouched: withheld placeholder
      recovery path (#1203 round-1 C1), probe-error "cannot determine" routing to
      `forcing_version_row_absent` (#1203 round-2 V5-C2), no probe fault escaping
      the pass (#1365 round-1), single manifest-key derivation through
      `_package_manifest_probe_uri`.

## 4. Green invariants

- [x] 4.1 A strict-warm-start restart whose own forcing package exists emits the
      same decision and `restart_stage` as before.
- [x] 4.2 A recorded reference naming the candidate's own model is probed
      unchanged.
- [x] 4.3 Existing `strict-warm-start` and `production-scheduler-orchestration`
      tests still pass.

## 5. Evidence Floor

- [x] 5.1 `uv run pytest -q tests/test_production_scheduler.py`
- [x] 5.2 `uv run pytest -q tests/test_orchestration_chain.py`
- [x] 5.3 `uv run ruff check .`
- [x] 5.4 `openspec validate enforce-per-model-forcing-completeness --strict --no-interactive`
- [ ] 5.5 (未做，非决定性 oracle) node-22 corroboration (NOT the decisive oracle — 1.1/1.2 and 5.1/5.2
      are). This is pure candidate-decision Python, deterministically exercised by
      the regression fixtures; a live pass adds deployment corroboration, not
      proof. Run one bounded scheduler pass from a throwaway clone (NEVER move
      `/scratch/frd_muziyao/NWM` onto a feature branch, and never pause the
      production timer without the stop-and-drain pattern) against a fixture
      registry naming a `model_id` with no forcing package for a completed cycle;
      capture the pass evidence showing the candidate `blocked` with a named
      reason and a `forcing_provenance` annotation, and no forecast submission.

## 5b. Round 1 review repairs

- [x] 5b.1 Pin BOTH halves of the identity pair. The three tests exercising
      `_recorded_forcing_reference_binds_candidate` discriminate only on
      `model_id`; a regression narrowing the predicate to
      `segments[-1] == model_id` passes the entire suite (verified empirically:
      88 predicate invocations across 3057 tests, 0 divergences between the
      shipped predicate and the weakened one). Add the missing case — two
      segments present, `basin_version_id` WRONG, `model_id` RIGHT — and assert
      it does not bind.
- [x] 5b.2 Add the matching spec scenario; the gap traced back to the acceptance
      criteria, which stated the requirement as a pair but named no
      `basin_version_id`-only mismatch in any scenario.
- [x] 5b.3 Correct the PR body's deviation ledger: the rewritten-reference group
      is 3 (not 4) in `tests/test_production_scheduler.py` plus 1 in
      `tests/test_orchestration_chain.py`, so the total is 21, not 22; and the
      shared helper has 39 in-file call sites (59 repo-wide), not 40.

## 6. Write-back

- [x] 6.1 Update issue #1826 with the measured scope correction (new-basin
      onboarding does NOT reproduce; re-identification does), the pass-date
      correction (the failing pass ran 2026-08-24T06:36:57Z, not 08-23), and the
      now-filled `Verification:` field.
- [x] 6.2 (#1845) File the two reported-not-fixed seams
      (`chain_forecast_cycle.py:490` unscoped cycle-wide job fallback,
      `:523` model-blind `job_matches_stage`) as a tracked issue.
- [x] 6.3 (#1846) File the "auto re-enter the forcing stage instead of blocking"
      follow-up as a tracked issue, referencing this change's Decision section.
