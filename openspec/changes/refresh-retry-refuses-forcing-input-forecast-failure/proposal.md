# Refresh retry refuses a forcing-input forecast failure (#2719)

```text
Issue type: bugfix
Fixture level: compact
Upstream suggested level: absent
Blast radius: a candidate that should be blocked for a human keeps one automatic retry (today's bug), or a
  candidate that should keep its model-package refresh retry is blocked (regression to avoid)
Selected risk packs: Error handling / retry policy; Legacy compatibility (others not selected, see tasks.md)
Evidence floor: new regression tests red before / green after; the two existing changed-model-package tests in
  tests/test_production_scheduler.py and tests/test_manual_retry_failed_stage_restart.py still pass; ruff
```

## Why

`_model_package_refresh_retry_evidence` (`services/orchestrator/scheduler_state_failure.py`) grants one automatic
retry, restarting at `forecast`, when a permanent failure coincides with a changed model package. It returns before
the permanent-failure guard in `scheduler_state_decision.py`. It never asks whether the failure is the runtime
rejecting the forcing package. A changed model package cannot repair a corrupt, unreadable or missing forcing
package, and a restart at `forecast` re-stages the same package, so the retry fails the same way, spends one Slurm
submission and one retry attempt, and delays the hand-off to an operator by one pass. #2670 fixed the same defect on
the manual lane; this channel predates the concept and was not covered. The issue's conclusion came from reading the
code; the first task turns it into an executable reproduction.

## What changes

- `_model_package_refresh_retry_evidence` returns `None` when `_failed_stage(state) in NATIVE_SHUD_STAGE_ALIASES`
  (the exact condition under which this channel emits a `forecast` restart, line 2048 — not the manual lane's
  `_canonical_downstream_stage`, whose alias set differs) and `_forcing_input_failure(state)` is true. The candidate then reaches the permanent-failure guard and is `blocked`,
  exactly as it is today when the model package has not changed.
- `_CHANGED_MODEL_PACKAGE_NON_CAUSAL_CLASSIFIERS` / `_CHANGED_MODEL_PACKAGE_NON_CAUSAL_CODES` are not edited (the
  #1161 / #1313 D2 line). The new check is a separate statement, not a row in that table.
- Chosen over "automatic full-chain retry without a restart stage" (issue alternative A): that would let a
  model-package change authorise a forcing regeneration it does not show to be needed, and would need a second
  strict-lane marker. Blocking matches what the main automatic lane already does for the same failure.

## Must preserve

- A non-forcing-input permanent forecast failure with a changed model package (for example `INVALID_MANIFEST`,
  `SHUD_FAILED`) still gets `retry_after_model_package_refresh` with `restart_stage == "forecast"`.
- `OUT_OF_MEMORY` / `resource_configuration` stay refused by the existing table.
- A forcing-input code recorded for a failure at a stage other than `forecast` is not affected by the new check.
- `DIRECT_GRID_TSD_FORC_TOO_LARGE` and its `_FORC_` siblings are not forcing-input codes (matcher unchanged).
- A forcing-input code at a failed stage outside `NATIVE_SHUD_STAGE_ALIASES` (for example `failed_stage="forcing"`
  with `FORCING_FAILED`) keeps today's refresh retry, which restarts at that stage and so regenerates forcing.
- Downstream consumer `services/orchestrator/chain_runtime_utils.py:268-275` recognises the refresh retry by
  `decision == "retry_after_model_package_refresh"` / `override_reason`; a blocked candidate never reaches it and
  the shape of the refresh evidence is unchanged.
- Known and accepted: `_forcing_input_failure` also reads the broad error-code scan, so a stale forcing code in
  the state can refuse a refresh retry that would otherwise be kept. The top-level `error_code` wins when present;
  pinned by a preserve test (1.3).
- The manual retry lane (#2670), the main automatic lane, the raw-input and downstream-resume pre-guard channels.

## Non-goals

No change to `failure_classifier`, `TRANSIENT_ERROR_CODES`, the `_forcing_input_failure` matcher, or the strict
warm-start escalator. `design.md` is omitted (compact fixture).
