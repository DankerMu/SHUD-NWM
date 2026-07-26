# Tasks — journal-predecessor-identity-quarantine (#1107)

Fixture level: compact (M scale: pure helper + light accessor + 2 wiring
points + tests; production scheduler readiness surface).

Risk triage: readiness-scoring change on the production scheduler; the risk
axis is silently changing skip semantics for well-formed or legacy journals
(env=false fallback warm starts are LEGAL and must never be quarantined —
criterion narrowed to same-base-key/different-lineage-suffix). Seams under
test: the `_build_candidates` seam (Wiring A, end-to-end),
`cycle_completion_status` / backfill gap selection (Wiring B), and
quarantine convergence (re-run exits the quarantine) and the ACCEPTED
non-convergent residual (env=false deterministic base-key re-selection →
permanent quarantine + head-of-line backfill slot occupancy; pinned by
3.5(b), rationale in proposal Design decisions). Risk packs: contract pack
(skip/backfill contract, spec alignment) + test-integrity pack (red-proof
discipline, single-value pins). Not selected: perf/security packs — cost
is one memo-fingerprint stat round + one `candidate_factory` construction
per completed model row (order lookback × sources × models), judged
acceptable; no trust boundary.

## 1. Helper + accessor (implementer)

- [x] 1.1 `services/orchestrator/scheduler_generation.py`: add pure TOTAL
  function
  `journal_init_state_lineage_matches_expected(recorded_init_state_id:
  str | None, *, source_id: str, model_id: str, candidate_valid_time:
  datetime, required_lead_hours: int) -> bool | None`. Polarity is part
  of the contract: `True` = matches expected token, `False` = positive
  mismatch (quarantine trigger), `None` = no judgement — the name states
  the True-polarity so `if not helper(...)` misreads are impossible;
  docstring restates all three values. Semantics:
  - Compute expected token via `packages.common.state_manager`'s module
    surface (`state_snapshot_id`, and the cycle-id derivation the module
    already re-exports/uses — do NOT import `workers.data_adapters.base`
    directly from this module) with expected predecessor coordinates:
    `valid_time` = candidate cycle time, predecessor cycle =
    `candidate_valid_time − required_lead_hours`, `lead_hours` = required.
    Also compute the expected BASE prefix (token minus lineage suffix —
    i.e. the suffix-less form for the same source/model/valid_time).
  - `recorded == expected token` → `True`.
  - recorded starts with the expected base prefix AND carries a non-empty
    lineage suffix different from the expected one → `False` (positive
    mismatch — the §8.7 cycle/lead misalignment class).
  - ALL other shapes → `None` (no judgement): None/empty, suffix-less
    legacy id equal to the base prefix, different base key (incl. earlier
    valid_time fallback states), malformed strings. Any
    `ValueError`/`TypeError` during token construction/comparison is
    caught → `None`; the function never raises. Import only stdlib +
    `packages.common` (module today imports no orchestrator modules —
    keep it that way).
- [x] 1.2 `services/orchestrator/file_orchestration_journal.py`: add
  `completed_pipeline_init_state_id(*, source_id, cycle_time, model_id) ->
  str | None` on `FileOrchestrationJournalRepository`, reading the same
  memoized `_cycle_rows` latest-view rows `has_completed_pipeline`
  (`:486-513`) uses; return the completed hydro_run's `init_state_id` (or
  `None` when absent / no completed entry / `hydro_run` is None — incl.
  the `state_save_qc` terminal mode where completion is decided from
  pipeline jobs, `:501-511`). NO run-manifest reads. IO errors follow the
  same fail-shape as `has_completed_pipeline`'s neighborhood — return
  `None` rather than raising for missing/unreadable rows it already
  tolerates. Docstring notes the accessor is consumed via `getattr` by
  scheduler wiring (repo convention, cf.
  `scheduler_backfill_predecessor.py:226`); NO Protocol change to
  `ActiveCandidateRepository`.

## 2. Wiring (implementer)

- [x] 2.1 Wiring A — `services/orchestrator/scheduler_candidates.py`
  terminal-skip else-leg (`:384-459` neighborhood): gate STRICTLY to
  completed-type `state_decision.reason` in `{terminal_hydro_success,
  terminal_pipeline_success, terminal_completed_cycle}` — NEVER
  `active_duplicate_pipeline` or any non-completed reason. Extract the
  recorded id from the in-scope `raw_candidate_state` (`hydro_run` →
  `init_state_id`, using the file's existing access idioms) and call the
  helper. On `False` (positive mismatch): REPLACE `state_decision` with
  `CandidateStateDecision("retry", "journal_predecessor_identity_mismatch",
  <evidence dict incl. recorded and expected tokens>)` following the
  sibling mismatch pattern at `:427-431` — do NOT merely skip the append;
  the replacement is what prevents the generic skip re-check at `:927-939`
  from re-skipping the candidate, and the evidence flows out via
  `_candidate_with_state_evidence` (`:1778`) at `:750-755`. On
  `True`/`None`: existing behavior byte-identical.
  `terminal_completed_cycle` inclusion is deliberate (only env=true-
  reachable completed skip with no identity gate; safe under the narrowed
  criterion) — record it in the report.
- [x] 2.2 Wiring B — `services/orchestrator/scheduler_discovery.py`
  `cycle_completion_status` completed-provider-only branch (`:184-198`)
  ONLY (the same-shape fallback `:224-233` is dead code — NOT wired): a
  model counts toward "complete" only if the helper does not return
  `False` for its recorded id. Add optional field
  `required_lead_hours_for_candidate: Callable[..., int] | None = None`
  to `SchedulerDiscoveryContext`; bind it in `scheduler_core.py` context
  construction (`:499-517`) to `self._required_warm_start_lead_hours`
  (no second copy of lead derivation). That bound method's signature is
  `(candidate, cycle)` (`scheduler_core.py:750-754`) and the
  completed-only branch holds only `discovery`/`models` — construct the
  arguments inside the branch following the `:141-144` pattern:
  `context.candidate_factory(...)` +
  `SchedulerSourceCycle(discovery=discovery, horizon=dict(horizon or {}))`,
  then call the bound callable. Field `None` or accessor missing
  (`getattr(context.active_repository, ..., None)`) → no judgement →
  legacy behavior.
- [x] 2.3 Constraints: no mutation/deletion of any journal file (read-only
  invariant); `has_completed_pipeline` itself unchanged; no changes to
  `chain_forecast_trigger.py`, `scheduler_generation_gate.py:95-124`, the
  D8.9 preflight signature, or `scheduler_adapters.py` Protocols.

## 3. Tests (implementer; red-provable)

- [x] 3.1 `test_stale_lineage_journal_entry_does_not_suppress_backfill` in
  `tests/test_scheduler_backfill.py` (the seam that exercises
  `cycle_completion_status` → `_select_backfill_source_cycles`): seed a
  completed journal latest-view entry for cycle T whose hydro_run
  `init_state_id` is a well-formed token with the SAME base key
  (source/model/valid_time=T) but WRONG lineage suffix (cycle_id one
  cadence off), using the `tests/test_file_orchestration_journal.py:148-197`
  `_latest_view` + `_write_json` pattern (that helper omits
  `init_state_id` — set it explicitly). Assert: (a) T is not reported
  "complete" (single-value pin); (b) journal file byte-identical
  before/after the scan (read-back comparison — immutability AC); (c)
  backfill gap selection includes T.
- [x] 3.2 `test_matching_lineage_journal_entry_still_skips_completed_cycle`:
  same seeding with the CORRECT expected `init_state_id` (computed via
  `state_snapshot_id`) → cycle reported complete / skip unchanged
  (single-value pin on the existing skip reason).
- [x] 3.3 No-judgement legs (parametrized or separate): (a) `init_state_id`
  absent → skip preserved; (b) suffix-less legacy id equal to the base
  prefix → skip preserved; (c) DIFFERENT base key — an earlier-valid_time
  fallback warm-start token (the legal env=false fallback,
  `chain_forecast_state.py:187-241` class) → skip preserved, NO
  quarantine. Leg (c) is the P1-3 false-positive pin and is mandatory.
- [x] 3.4 Wiring A leg at the `_build_candidates` seam in
  `tests/test_scheduler_generation.py` (env=false, strict None via
  D8.9-completed preflight): completed entry with same-base-key
  wrong-suffix recorded id → candidate NOT skipped as terminal-duplicate;
  `state_decision` pinned as
  (`"retry"`, `"journal_predecessor_identity_mismatch"`) with evidence
  containing recorded and expected tokens (single-value pins). Reuse the
  T15-family fixture helpers (`_set_db_free_scheduler_env`,
  `_write_db_free_file_provider_fixtures`,
  `_write_db_free_state_index_fixture`).
- [x] 3.5 Convergence legs (both mandatory): (a) after 3.1's quarantine,
  simulate the re-run by rewriting the latest-view entry with the CORRECT
  expected `init_state_id` → next `cycle_completion_status` pass reports
  T complete and T leaves the backfill gap set (quarantine exited by one
  re-run). (b) Accepted-residual pin: rewrite with the SAME wrong-suffix
  id (the deterministic env=false base-key re-selection class,
  `chain_forecast_state.py:662-665` + `state_manager.py:981-1010`
  `min(state_id)`) → T is still not complete, remains in the gap set, and
  would be re-selected (head-of-line, `available_gaps[:1]`) — the
  permanent-quarantine loop is pinned in-test, not left for production
  discovery (proposal Design decisions records the acceptance rationale).
- [x] 3.6 Red proof: 3.1 and 3.4 must FAIL on the unwired code for the
  right reason (cycle reported complete / candidate skipped), captured by
  stashing the wiring hunks; record red output in the report → PR body.
- [x] 3.7 env non-regression: existing strict-comparator and
  fallback-warm-start tests stay green untouched
  (`test_scheduler_generation.py`, `test_production_scheduler.py`,
  `test_scheduler_backfill_predecessor.py`); no new env=true leg needed
  beyond 3.3(c)+existing coverage IF nothing red-flags — state this
  explicitly in the report if so.

## 4. Verification (orchestrator)

- [x] 4.1 `uv run pytest -q tests/test_scheduler_backfill.py tests/test_scheduler_backfill_predecessor.py tests/test_scheduler_generation.py tests/test_file_orchestration_journal.py tests/test_state_manager_generation_history.py tests/test_production_scheduler.py` all green.
- [x] 4.2 `uv run ruff check .` clean.
- [x] 4.3 `openspec validate journal-predecessor-identity-quarantine --strict --no-interactive` valid.

## 5. Spec delta (orchestrator, this fixture)

- [x] 5.1 ADDED requirement (new title — umbrella §8.7 lives as unarchived
  ADDED in `node22-db-free-scheduler-state`; same-title ADDED would collide
  at archive): scenarios for positive-mismatch quarantine (not complete +
  backfill not suppressed + entry immutable), matching-id skip preserved,
  missing-id legacy skip preserved, different-base-key fallback NOT
  quarantined.

## Evidence mapping (issue AC → tasks)

- AC test "stale lineage does not suppress backfill" (journal unmutated +
  not-canonical-ready + backfill emitted) → 3.1 (+3.4 Wiring A leg)
- AC test "matching lineage still skips" → 3.2
- Fallback/legacy never quarantined (P1-3 risk axis) → 3.3
- Quarantine converges on re-run (P1-4 risk axis) → 3.5(a)
- Accepted non-convergent residual pinned (P1-A, option iii) → 3.5(b) +
  follow-up issue routed at Phase 8 (quarantine breaker /
  lineage-preferring exact selection under env=false)
- AC existing suites keep passing → 4.1
- AC ruff + validate → 4.2/4.3 (issue names `openspec validate
  node22-db-free-scheduler-state`; that change is untouched by this PR so
  its validate result is unchanged — run it once in Phase 2 as a
  confirmation, not a gated artifact of this change)
- Issue "建议方案" deviations: helper compares narrowed lineage-suffix
  class instead of a `PredecessorIdentity` typed dict with `generation`
  (journal records no generation — recon-verified; generation half
  explicitly out of scope, known limit in proposal); helper lives in
  `scheduler_generation.py` not the journal module (circular-import
  safety); full-token inequality rejected as mass-false-positive under
  env=false fallback warm starts (fixture-review P1-3).
