# Journal-side predecessor-identity quarantine filter for §8.7 (#1107)

## Why

Spec §8.7 ("Stale-lineage journal entries do not suppress backfill", live in
`openspec/changes/node22-db-free-scheduler-state/specs/file-state-snapshot-index/spec.md:152-166`)
requires that a completed cycle-T journal entry whose recorded predecessor
identity does not match the required predecessor of the current generation be
treated as not-canonical-ready, without suppressing backfill and without
mutating the entry. Three completed-skip paths exist with no identity check
(fixture-review verified; the strong comparator
`_terminal_decision_matches_strict_warm_start` at
`scheduler_candidates.py:1803-1829` fires only when `strict_warm_start`
evidence exists):

- **Gap A** — `scheduler_candidates.py:384-459` terminal-skip else-leg:
  covers (a) `terminal_hydro_success`/`terminal_pipeline_success` with
  `strict_warm_start is None` (D8.9 preflight nulls it under
  `NHMS_REQUIRE_FORECAST_WARM_START=false` + journal-completed,
  `scheduler_core.py:740-748`), and (b) `terminal_completed_cycle`
  (`scheduler_state_decision.py:207-208`) which is reachable under BOTH env
  values and has never had any identity gate. (The third path,
  `completed_duplicate_pipeline` at `:329-337`, is production-unreachable —
  its predicate requires `not callable(state_provider)`, false in db-free —
  and is left untouched.)
- **Gap B** — `scheduler_discovery.py:184-198` (`cycle_completion_status`
  completed-provider-only branch): returns "complete" from
  `has_completed_pipeline` alone; `_select_backfill_source_cycles:357-412`
  then removes T from backfill gaps. (The same-shape fallback at `:224-233`
  is dead code — reachable only when the main branch already returned — and
  is NOT wired.)

## What Changes

- **Pure total-function helper** in
  `services/orchestrator/scheduler_generation.py`:
  `journal_init_state_lineage_matches_expected(recorded_init_state_id, *,
  source_id, model_id, candidate_valid_time, required_lead_hours) ->
  bool | None` (`True` = matches, `False` = positive mismatch, `None` =
  no judgement — name states the True-polarity so wiring conditions read
  correctly). Judgement semantics (narrowed to the §8.7 cycle/lead
  misalignment class — see Design decisions):
  - Compute the expected token via
    `packages.common.state_manager.state_snapshot_id` with the expected
    predecessor coordinates (`valid_time` = candidate cycle time,
    `cycle_id` = cycle_id_for(source, cycle − lead) — accessed via
    `state_manager`'s module surface, NOT by importing
    `workers.data_adapters.base` directly — `lead_hours` = required), and
    the expected base prefix (token minus lineage suffix).
  - Recorded == expected token → `True` (match).
  - Recorded shares the expected BASE key (same source/model/valid_time
    prefix) but carries a DIFFERENT non-empty lineage suffix → `False`
    (positive mismatch).
  - Everything else → `None` (no judgement): missing/empty id, legacy
    suffix-less id, different base key (e.g. an EARLIER `valid_time` — the
    legal env=false fallback warm start, `chain_forecast_state.py:187-241`,
    which may select `get_latest_usable_state(before_time=...)`), or any
    token-construction error (`ValueError`/`TypeError` caught → `None`;
    the helper never raises).
- **Light journal accessor** `completed_pipeline_init_state_id(*, source_id,
  cycle_time, model_id) -> str | None` on
  `FileOrchestrationJournalRepository`, reading the same memoized
  `_cycle_rows` rows `has_completed_pipeline` (`:486-513`) uses. Consumed
  via `getattr(repo, ..., None)` + docstring note (repo convention, e.g.
  `scheduler_backfill_predecessor.py:226`); NO Protocol change (the
  `ActiveCandidateRepository` Protocol has no optional-member precedent).
  Stubs without it → no judgement → behavior unchanged.
- **Wiring A** (`scheduler_candidates.py` terminal-skip else-leg): gated to
  completed-type reasons only — `{terminal_hydro_success,
  terminal_pipeline_success, terminal_completed_cycle}`; NEVER
  `active_duplicate_pipeline` (that would resubmit over a running
  pipeline). On helper `False`: REPLACE `state_decision` with
  `CandidateStateDecision("retry", "journal_predecessor_identity_mismatch",
  <evidence>)` following the sibling mismatch pattern at `:427-431`, so the
  later generic skip re-check at `:927-939` cannot re-skip it and the
  evidence flows out via `_candidate_with_state_evidence` (`:1778`) at
  `:750-755`. `terminal_completed_cycle` is INCLUDED deliberately: it is
  the only completed-type skip reachable under env=true with no identity
  gate; the narrowed criterion makes this safe (strict-path selections
  match the expected token by construction).
- **Wiring B** (`scheduler_discovery.py:184-198` only): a model counts
  toward "complete" only if the helper does not return `False` for its
  recorded id. `required_lead_hours` comes from a NEW optional
  `SchedulerDiscoveryContext` field
  (`required_lead_hours_for_candidate: Callable | None`), bound in
  `scheduler_core.py` (`:499-517` context construction) to
  `self._required_warm_start_lead_hours` — no second copy of lead
  derivation; field `None` → no judgement.
- **Tests** (red-provable) + **ADDED spec requirement** (new narrower
  title).

## Design decisions

- **Criterion = same base key, different lineage suffix** (not full-token
  inequality). Under env=false — the only regime where Gaps A(a)/B are
  reachable — the warm-start selector legally falls back to earlier or
  suffix-less states; full-token inequality would quarantine those
  well-formed runs en masse (fixture-review P1-3). The narrowed criterion
  captures exactly §8.7's "same cycle-T state slot, wrong
  predecessor cycle/lead" class, and every excluded shape keeps legacy
  behavior. Known limits (recorded explicitly): (1) the generation half
  (same key+suffix, different `package_checksum`) is not in the journal —
  covering it needs run-manifest reads or a write-side field, out of scope
  (strong comparator still covers it when strict evidence exists); (2)
  suffix-less legacy recordings at the same valid_time are treated as
  legacy, not stale; (3) under `state_save_qc` terminal mode
  (`file_orchestration_journal.py:501-511`) completion may be decided from
  pipeline jobs with `hydro_run=None` → accessor `None` → no judgement.
- **Convergence and the accepted residual (fixture-review P1-A, option
  iii)**: a quarantined cycle re-enters gap selection
  (`selected_for_source = available_gaps[:1]`, oldest-first,
  `scheduler_discovery.py:386`). When it re-runs, the run records a new
  `hydro_run.init_state_id`: the exact token (→ matches → complete) or a
  different-base-key fallback (→ no judgement → complete) both exit the
  quarantine in one re-run (test 3.5(a)). One class does NOT converge and
  is ACCEPTED as a known risk: under env=false the exact-state lookup
  (`chain_forecast_state.py:662-665`) passes no `cycle_id`/`lead_hours`,
  so `state_manager.py:981-1010` deterministically picks
  `min(entries, key=state_id)` over the base key — when the nominal
  predecessor cycle never ran and an entry at `valid_time=T` from an
  earlier cycle exists, every re-run legally re-selects that same
  wrong-suffix state, the cycle is re-quarantined each round, occupies
  the source's single oldest-first backfill slot, and is resubmitted once
  per round. No §8/§8.6 machinery resolves this population: the D8.9
  preflight (`scheduler_core.py:743-747`) returns `None` for env=false +
  journal-completed, so no §8 blocked evidence and no predecessor
  emission are ever produced there. Accepted because: (a) the loop is
  operator-visible via the typed retry reason
  `journal_predecessor_identity_mismatch` with recorded+expected tokens
  in evidence; (b) it is bounded to one backfill slot per source; (c) the
  alternative — an existence precheck on the expected token — adds
  state-index IO to the judgement path yet still does not converge, since
  `min(state_id)` orders the older cycle's suffix first even when the
  expected entry exists. Reachability note: under the current deployment
  cadence (`NHMS_SCHEDULER_ALLOWED_CYCLE_HOURS_UTC=0,12` → checkpoint
  lead-interval set `{12}` via
  `chain_manifest_contracts.py:426-442` → one checkpoint per run → at
  most one entry per base key) this residual class is UNREACHABLE; it
  requires a multi-interval cadence config (e.g. `0,6,12`). Test 3.5(b)
  pins the loop shape explicitly; a follow-up issue (routed at Phase 8,
  trigger precondition = multi checkpoint-lead configs) will track a
  quarantine breaker / lineage-preferring exact selection under
  env=false.
- **Fail-shape**: helper is total (never raises); accessor mirrors
  `has_completed_pipeline`'s tolerance for missing/unreadable rows
  (→ `None`). This surface can only DECLINE to skip — it cannot admit —
  so "error → no judgement → legacy skip" does not reintroduce a
  #1150-class fail-open.
- **Read-only invariant**: no journal mutation/deletion anywhere.

## Impact

- Affected specs: `file-state-snapshot-index` — ADDED requirement
  "Completed-cycle skips SHALL be gated by journal-recorded predecessor
  identity" (new narrower title; umbrella §8.7 is an unarchived ADDED in
  live change `node22-db-free-scheduler-state`, same-title ADDED would
  collide at archive; that change's tasks.md already routes this filter to
  #1107 — no edits there).
- Affected code: `scheduler_generation.py` (helper),
  `file_orchestration_journal.py` (accessor), `scheduler_candidates.py`
  (Wiring A), `scheduler_discovery.py` + `scheduler_core.py` (Wiring B +
  context field), tests.
- Must preserve: matching-token and no-judgement shapes skip exactly as
  before; `active_duplicate_pipeline` untouched; env=true strict comparator
  path untouched; stub repositories unchanged; journal files byte-identical
  after scans; `has_completed_pipeline` semantics unchanged for
  `chain_forecast_trigger.py:378` and `scheduler_generation_gate.py:95-124`;
  `cycle_completion_status`'s only consumer is backfill gap selection
  (verified) so no other surface shifts.
- Perf: Wiring A reuses in-scope `raw_candidate_state` (zero new reads);
  Wiring B adds one accessor call per completed model row — no new file
  reads, one extra memo-fingerprint `os.scandir`/stat round per call
  (`_cycle_rows_source_fingerprint`, `:3547-3577`).
- Non-goals: generation-aware identity; journal structure/retention;
  state-index-side logic (R1); auto-trigger surface; D8.9 preflight
  signature; the production-unreachable `completed_duplicate_pipeline`
  branch; the dead fallback `scheduler_discovery.py:224-233`.
