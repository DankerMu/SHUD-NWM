# Design — cohort-identity-manifest-contract

Fixture level: **expanded**. The change touches retry budget accounting, accepted-submit release, cross-cohort conflict detection, the §8.7 breaker and a public schema contract. All paths are file-journal / db-free; the DB path must stay byte-identical.

## #2542 per-member cohort charge

- **Write side.** In `chain_forecast_orchestrator_cycle._reserve_cycle_stage`, a forecast cohort master reservation records `retry_attempt_floors`. It is written only for the forecast stage and only when at least one active basin carries a floor. It is a list of `{model_id, attempt}` for each member whose `retry_attempt_floor(state_evidence, stage)` is not None. The list is bounded (≤ MAX_FORECAST_COHORT_MEMBERS), sorted and de-duplicated by `model_id`, with non-negative ints.
- **Constant and normaliser.** Define both in `retry_identity.py` (which owns `RETRY_ATTEMPT_FLOOR_FIELD`); the frozen-set registration stays in `accepted_submit_identity.py`. Follow `QUARANTINE_RERUN_PROVENANCE_FIELD` / `normalize_quarantine_rerun_model_ids`.
- **Closed constructor.** Register the field as an explicit member of the journal master's closed constructor (`file_orchestration_journal.py` ~9524, next to `BUDGET_REENTRY_PROVENANCE_FIELD`).
- **Freezing.** The field is first-write-frozen, like the other provenance fields: reclaim and re-entry never overwrite it.
- **Mint unchanged.** `_floored_cycle_stage_retry_job_id` still mints `max(floors)+1`, skipping occupied ids forward (#1201).
- **Reconcile.** Where reconcile builds the per-model row (`file_orchestration_journal.py` ~5462), set each member's `retry_count` from `floors = master.retry_attempt_floors` and `eff = effective_retry_attempt(master)`:
  - **`floors` absent or empty** (legacy master, a cohort with no floored member, or a one-member cohort without a floor): `eff`, the current shared charge. This must be byte-identical to origin. The normaliser turns absent into `[]` and the closed constructor writes the key on every row, so this branch must key on emptiness, not on key presence.
  - **Otherwise:** `min(eff, own_base + (eff - (max(floors) + 1)))`.
    - `own_base = own_floor + 1` for a listed member; `own_base = 0` for an unlisted member.
    - **Guard:** if `eff < max(floors) + 1` (a path that reuses an existing id without the floored minter, e.g. a non-terminal existing row at `chain_forecast_execution.py` ~255-266 or an operator-verified-absence recovery), fall back to the shared charge `eff` (fail-closed). Otherwise the difference `eff - (max(floors)+1)` is ≥ 0. It carries every further attempt the master itself consumed: an inline retry in the same call (`_retry_2` → `_retry_3`, `tests/test_retry_mint_floor.py:422-462`, where `_reserve_cycle_stage` recomputes the same floors), or an occupied id skipped forward. So each member's charge advances with each of its submissions. It over-counts only on skips, which fails closed, as origin does.
    - A one-member cohort's own floor is the max, so its charge is exactly `eff`. In the file journal every forecast master is model-less, so single-model runs go through this path, and its charge stays unchanged (must-preserve).
  - **Why unlisted → base 0 does not forget attempts.** The budget reads the max over all of a candidate's authoritative rows (`scheduler_state_rows.py:615-627` and the #1179 floors). An unlisted member already charged 2 under an older prefix still reads 2 next pass. This needs a pin test.
- **Registration points.** All four must include the field; missing any one silently drops the value.
  - The `_pipeline_job_row` closed constructor (~9524).
  - `ACCEPTED_SUBMIT_MASTER_ORDINARY_UPSERT_FIELDS` frozen set (`accepted_submit_identity.py` ~323).
  - The merge tuple (`file_orchestration_journal.py` ~541-560).
  - `normalize_accepted_submit_evidence` (~876).
- **Manual-retry clone boundary.** Manual retry clones `**failed_job` (~11698) and strips contract markers (~11718-11722). It must also strip `retry_attempt_floors`, so a cloned pending row never carries stale floors. Alternatively, prove that reconcile cannot read them there; either way, add a test.
- **Caveat to verify.** Check whether `retry_attempt_floor` semantics are "attempts already charged" (next = floor+1) by reading `scheduler_state` producer and `tests/test_retry_mint_floor.py`. The issue's AC geometry (A floor 0 → retry_count 1, B floor 1 → 2 with limit 2 → A retry / B blocked; limit 3, A 0 / B 2 → A 1, B 3) is the oracle.
- **Spec.** Modify the job-retry-mechanism requirement's last sentences and the "Mixed-floor" scenario (it is no longer deferred).

## #2557 hydro_run attempt refresh (triage first)

- **Red test first.** Go through the real path:
  1. `orchestrate_cycle`, or `create_hydro_run_from_basin` via `chain_manifests` forecast staging.
  2. First attempt; hydro row succeeded or active.
  3. Reservation reclaim, or a same-`run_id` second pass whose reservation `submission_attempt=2`.
  4. Second staging.
  5. Assert the durable hydro row's `submission_attempt`.
  6. Then drive `reject_pipeline_job_submit_attempt` / `permit_pipeline_job_retry` / `demote_operator_verified_reserved_job` for attempt 2, and assert the active hydro row is released to `failed`.
- **If the test reproduces:**
  - In `create_hydro_run_from_basin` only (`create_hydro_run` raises on NOT_RETRIABLE and serves trigger/analysis; it is out of scope), the refresh happens **inside `_write_hydro_run`'s journal lock**. This can be a dedicated `_refresh_hydro_run_attempt` that re-reads the existing row under the lock.
  - It appends a new record only when `existing.submission_attempt < manifest attempt`, and changes only `submission_attempt` and `run_manifest_uri`.
  - It must not write back a copy read outside the lock: the current catch branch re-reads `_hydro_run_for` unlocked, and writing back from that read could revert a concurrent `update_hydro_run_status`.
  - It never lowers the attempt, and never changes `status`, init-state identity (#2397/#2538), or any other field.
  - The returned row is the refreshed row.
- **If it does not reproduce:** add a test/comment pinning why, and leave the code unchanged. #2557 stays open with a comment.
- **Unchanged:** the three release entrypoints keep their `attempt ==` guard (must-preserve: never release another attempt's row).

## #2546 convert members

- **Change.** Add `convert` to `_MEMBER_RECORDING_DOWNSTREAM_STAGES` in `chain_forecast_orchestrator_cycle.py`. The existing gating stays: `supports_accepted_submit_reconcile`, model-less row, `reservation_evidence is None`. Members get `restart_stage="convert"`.
- **Readers, verified stage-gated by exploration:**
  - `forcing_submit_identity.py`
  - `reconcile.py` ~1612 (forecast only)
  - `chain_array_accounting.py` ~328
  - `_accepted_submit_structural_row_kind` (gated on `accepted_submit_contract_is_current` + forecast stage)
- **Reader tests.** A test pins that a convert row carrying members is `legacy` / `accepted_submit_row_kind is None`, and is not read as forcing identity.
- **Candidate-state interplay (PR #2656).**
  - A convert row with members is classified `member` / `non_member`.
  - `convert` is not in `COHORT_MEMBER_ATTRIBUTED_STAGES`, so a `member` convert row keeps its origin (unattributed) semantics.
  - A `non_member` convert row, which belongs to a sibling cohort, is dropped from the candidate state. That drop is the #2546 residual risk and is fixed as a side effect.
- **Accepted deviation.** The issue suggested a new field `execution_member_model_ids`. We reuse `cohort_members` because every accepted-submit reader is stage-gated, and it keeps one membership field for `has_active_pipeline`. The test above pins this.

## #2555 dispatch order

In `scheduler_candidates.py`, the `terminal_run_manifest_missing` elif (~622) becomes:

1. Compute `identity_quarantine = _journal_predecessor_identity_quarantine(context, candidate, cycle, state_decision)`.
2. If it is not None, it replaces the decision and then consults the forcing witness, exactly as the existing else-leg does.
3. Otherwise, emit the existing `terminal_run_manifest_missing` retry, which also consults the witness.

The else-leg for manifest-present rows is unchanged. The strict lane is unreachable here (the first strict branch consumes it) and is out of scope. The DB lane is unchanged because `completed_pipeline_init_state_id` is file-journal-only, so the quarantine yields None there. Residual risk, not fixed: a manifest-missing loop whose lineage matches still has no retry budget. Spec: MODIFIED on the existing run-manifest-missing requirement, whose "with own forcing is unchanged" scenario now carries the "quarantine yields no decision" condition.

## #2539 manifest contract

- **Schema root.** Declare in `run_manifest.schema.json` root properties, with types matching what the builder writes:
  - `submission_attempt`: integer ≥ 1.
  - `candidate_id`, `workspace_dir`, `object_store_root`, `object_store_prefix`: strings.
  - `forecast_horizon_hours`: number.
  - `display`, `quality_states`, `residual_blockers`: shape-appropriate object/array.
  - `additionalProperties: false` stays.
- **Builder.** `build_forecast_runtime_manifest` writes top-level `schema_version: "1.0"`, matching the example. The value is additive and SHUD runtime ignores it.
- **`runtime.executable`.** Drop it from `runtime.required`, with a description that the executable resolves from `SHUD_EXECUTABLE`. Writing a value nobody reads would be a false contract.
- **Nullable fields.** Enumerate every field the builder can write as null: a cold start writes `ic_file_uri: basin.get("init_state_uri")`, and a packaged IC writes `state_id`/`ic_file_uri: None` (`chain_manifests.py` ~501-530). Widen those schema types to `["string","null"]`.
- **Validator.** `validate_forecast_runtime_manifest` keeps its behavior; it is the only caller, at staging `chain_manifests.py:380`. Its required paths (`workspace_dir`, `object_store_root`, …) become schema-declared. It does **not** add a runtime jsonschema check. Must-preserve: never reject a manifest the previous validator accepted. Schema conformance is enforced in tests.
- **FormatChecker.** Tests validate with `jsonschema` using `FormatChecker`, matching CI's check-jsonschema, which checks formats.
- **Old trigger path.** `build_forecast_run_manifest` (`chain_manifests.py` ~645-700, the trigger path) is **out of scope**. Its violations (no `schema_version`) are recorded in the PR only.
- **Large-file guard.**
  - `.claude/hooks/large-file-guard` blocks commits touching non-excluded files over 1000 lines. `chain_manifests.py` (1024) and `accepted_submit_identity.py` (1383) are not excluded.
  - Add both to `.large-file-guard.json` `exclude`, with a rationale, following the precedent of PR #2584 for `chain_forecast_orchestrator_cycle.py` and `chain_stage_execution.py`.
  - Put the new constant and normaliser in `retry_identity.py`, which owns `RETRY_ATTEMPT_FLOOR_FIELD`. The frozen-set registration still touches `accepted_submit_identity.py`.
- **Tests.**
  - `_PRE_EXISTING_UNDECLARED_MANIFEST_KEYS` shrinks to empty or is removed.
  - A new test validates the real `input/manifest.json` from db-free `orchestrate_cycle` passes (warm start, cold start, packaged IC) against the schema, and fails when an undeclared key is injected.
  - `schemas/examples/run_manifest.example.json` stays valid; CI check-jsonschema.

## Must-preserve

- DB-lane behavior is unchanged.
- Single-model mint and charging are unchanged.
- The shared charge for legacy masters is unchanged.
- The release entrypoints' attempt guard is unchanged.
- `has_active_pipeline` still refuses a true duplicate.
- Manifest-present None-lane and strict-lane dispatch are unchanged.
- Manifest-missing rows without stale lineage get a byte-identical `retry_terminal_run_manifest_missing`.
- No key is removed from written manifests.
