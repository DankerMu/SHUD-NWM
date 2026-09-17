## Context

`_national_discharge_coverage_rows` is the single owner of the national discovery path's
display-ready predicate. It returns `(rows, active_network_ids)` and three consumers judge coverage
with it:

- `national_discharge_cycles` — per cycle, `covered_networks != active_networks` → skip the cycle;
- `national_discharge_cycle_coverage` / `NationalCycleCoverage.complete` — read by the per-cycle
  branch of `national_discharge_valid_times` and by the canonical national tile route (#2153);
- `_default_layer_catalog` (`apps/api/routes/hydro_display.py`) — calls the two consumers above once
  each, so it holds a third, outer snapshot seam that is explicitly out of scope here.

Today the helper executes the active-network statement first and the coverage statement second.

## Goals / Non-Goals

Goals:

- Make the active set and the covered set come from one consistency direction, so the two known
  fail-open activation branches become fail-closed.
- Zero behavior change in the absence of a race.

Non-Goals:

- The intersection semantics themselves (unchanged).
- The cycle lookback window (#2009), `run_display_coverage` freshness (#2080), the
  hydro-national tile digest/SQL mismatch (#2031), display-catalog cache public controllability
  (#2078 / #2079).
- The outer seam between `_default_layer_catalog`'s two calls: that is a different layer (two
  full helper invocations, not two statements inside one), and closing it needs a different fix.
- Any SQL text change. `tests/test_display_publish_status_only.py` pins the `h.status IN (...)`
  occurrence counts at 2/1/1/5; this change must keep both statements textually identical.

## Decisions

### D1: Swap the statement order (KISS), not a single-statement merge or REPEATABLE READ

Chosen: execute the coverage statement first (T1), the active-network statement second (T2).

Derivation (re-derived against the current SQL, not inherited from the issue):

- The coverage statement's inner query joins `core.model_instance mi` and filters
  `mi.active_flag AND mi.river_network_version_id IS NOT NULL` — the exact predicate pair the
  active-network statement uses. Therefore every returned row's network is active in T1's snapshot:
  `covered ⊆ active@T1`.
- Activation is monotone in the direction that matters: if a network `A` is activated between T1 and
  T2 then `A ∈ active@T2` and `A ∉ active@T1`, hence `A ∉ covered`, hence `covered != active@T2`.
- Branch (a), zero-coverage activation: `covered = {B,C}`, `active@T2 = {A,B,C}` → unequal → every
  cycle is skipped and `cycles == []`. Closed.
- Branch (b), partial-coverage activation: at T1, `A` is not yet active, so its cycle-K rows are
  filtered out by `mi.active_flag`; `covered_K = covered_J = {B,C}` while `active@T2 = {A,B,C}` →
  both cycles unequal → both closed. Closed. (Under the old order only K was caught, by #2073.)
- Version switch (deactivate `rnv1`, activate `rnv2`) lands unequal in both orders.

Rejected alternatives:

- **Single-statement merge** (`LEFT JOIN` the coverage subquery onto `core.model_instance`): one
  snapshot, closes the growth AND the shrink class at once — but it changes the returned row shape
  (zero-coverage networks produce all-NULL rows that `_national_coverage_window` and
  `_national_run_rank` would have to special-case) and risks a second `h.status IN (...)` SQL shape,
  which `tests/test_display_publish_status_only.py` pins. Cost is out of proportion to a p3 race.
- **REPEATABLE READ**: `SET TRANSACTION ISOLATION LEVEL` must be the first statement of the
  transaction, and `get_hydro_display_session` hands out a session that may already have executed;
  it would also introduce a new transaction convention on the read-only display plane, and would need
  node-27 verification. Rejected as disproportionate.

### D2: What the swap buys, what it costs, and what it newly opens

The swap is not strictly "closes races without opening any". It exchanges one class of race for
another, and both must be written down — in this file AND in the helper docstring — or the next reader
inherits a false oracle.

- **Closed (the point of the change): numerator-GROWTH races.** A network activated between the reads
  is in `active@T2` and not in `covered@T1`, so the comparison is unequal and every cycle fails
  closed. This covers both issue #2087 branches, zero-coverage and partial-coverage activation.
- **NEWLY OPENED by the swap: numerator-SHRINK races.** If a row that was in `covered@T1` stops being
  display-ready before T2, the old order caught it (`active@T1` still held the network, `covered@T2`
  did not → unequal → closed) and the new order does not (`covered@T1` and `active@T2` both hold the
  network → equal → the cycle is listed although its run is gone). This is a new fail-open branch, not
  a pre-existing one, and the earlier framing of it as "unchanged" was wrong.

  The shrink class DOES have reachable production writers; do not claim otherwise. Verified:
  - No production `DELETE FROM hydro.hydro_run` / `hydro.run_display_coverage` exists (only test
    teardown helpers and one change-local rehearse script), so deletion is not the live writer.
  - Status IS reachable backwards out of the display-ready set:
    `workers/output_parser/parser.py` `FAILABLE_RUN_STATUSES` includes `succeeded` and `parsed`, and
    `mark_run_failed` uses it as the WHERE guard; `workers/shud_runtime/runtime.py` `mark_failed` has
    no status guard at all. An already-parsed run holding a `run_display_coverage` row can therefore
    become `failed`. (`_TERMINAL_HYDRO_STATUSES` in `apps/api/routes/pipeline.py` is NOT a global
    state-machine guard — it only stops the cancel endpoint from overwriting a terminal row — so it
    must not be cited as one.)
  - `rdc.segment_count` can be driven to 0 only through the `packages/common/display_coverage.py`
    upsert, which #1446 already made refuse to zero a populated row (capability
    `display-coverage-freshness`).

  Why the trade is nonetheless the right call, argued on its real merits:
  - **Any two-statement design leaves exactly one class open.** A numerator shrink is invisible to any
    re-read of the denominator, so it can only be closed by re-reading the numerator or by collapsing
    to one statement. The choice is therefore which class to close, not whether to close both.
  - **Issue #2087 pre-accepted this residual** (it named it as the surviving branch and asked for the
    swap anyway); the only thing wrong in its framing was the "现有代码同样不覆盖" clause, which is
    false — the old order does catch shrink. That correction is reported back on the issue rather than
    silently rewritten here.
  - **Closing both costs an SQL rewrite with node-27 and test-fake fallout.** The single-statement
    merge (rejected in D1) rewrites one of the four run-selection sites the module header requires to
    agree with each other (`services/tiles/mvt.py` header: the three tile-side sites plus
    `_national_discharge_coverage_rows`' inner run-selection query). Under the mutation-matrix
    discipline of `invariant-matrix-i5-2009.md`, a row whose only local red signal is a predicate-text
    tripwire is a node-27-lane row needing a `Measured` cell from the real-DB integration file — and a
    `JOIN ... ON` predicate is exactly that, because `_NationalDiscoverySession` never executes SQL. So
    the merge pulls a node-27 oracle into a p3 fix. It also collapses the test fakes' statement classification:
    `_NationalDiscoverySession.execute` dispatches on `"core.model_instance mi" in sql and
    "hydro.hydro_run" not in sql`, and `_NationalRouteSession`'s `active`/`coverage` kinds, its
    `active_params` / `coverage_params` captures and every `_statement_kinds` assertion assume two
    statements.
  - **The window is identical for both classes** — two adjacent sub-millisecond statements on one
    session — so the swap does not widen anything.

  The implementer MUST re-verify these writer facts before writing the docstring rather than copying
  this list, and MUST state the newly opened class in the docstring in these terms — including that it
  has live writers, not that it is unreachable.
- **Deactivation-with-zero-coverage delta**: under the new order, a network that had no coverage rows
  and is deactivated between T1 and T2 lets the cycle list (`covered@T1 = {B,C}` equals
  `active@T2 = {B,C}`), where the old order refused it (`active@T1 = {B,C,D}`). This is the correct
  answer — a deactivated network does not need rendering — but it IS a race-path behavior delta and is
  recorded here so it is not mistaken for a regression. Deactivating a network that DOES have coverage
  rows stays fail-closed in both orders.
- **Availability cost**: one ordinary activation makes the intersection empty for as long as the
  freshly computed answer says so, and the display catalog can serve it for up to one fresh TTL (60 s)
  or, on the stale-while-revalidate path, up to `DISPLAY_CATALOG_STALE_MAX_SECONDS` (600 s). That is
  the module's declared fail-closed semantics (no data beats wrong data) and must be written into the
  helper docstring.

### D3: The race fake models "first statement sees pre-state, later statements see post-state"

The test fake must be order-independent so the same four cases are red before the swap and green
after. Two constraints, both load-bearing:

- The activation flips after the FIRST `execute()`, whichever statement that is.
- The coverage branch of the fake must filter its rows by the active set current at that execution —
  the real SQL does this with `mi.active_flag` inside the statement. Without it, branch (b) under the
  new order would return `A`'s cycle-K row from the T1 snapshot and the test would prove less than the
  SQL does.
- This belongs in a NEW subclass, not in the base `_NationalDiscoverySession`: the base deliberately
  ignores `active_networks` when answering the coverage query, and several existing tests pass an
  explicit `active_networks=` that is WIDER than their rows (the fail-closed fixtures). Adding an
  active-filter to the base would change those tests' semantics.

## Risks / Trade-offs

- Risk: a docstring/comment left describing the old statement order becomes an actively misleading
  oracle for the next reader. Mitigation: the fixture lists every stale site as a task.
- Risk: the fake diverges from the real SQL and a test passes vacuously. Mitigation: every case
  carries non-vacuity assertions (active set ≥ 3 members, coverage rows non-empty) and the four cases
  must be shown red against unmodified `services/tiles/mvt.py`.
- Trade-off: the swap opens the numerator-shrink class (D2), which has live writers. Accepted because
  no two-statement design can close both classes and closing both costs the plan-changing merge D1
  rejects — not because the class is unreachable, and not because it was already broken.
- Risk: a reviewer reads the swap as pure improvement and the residual as pre-existing. Mitigation:
  D2 states the new branch explicitly and the docstring must repeat it.

## Migration Plan

None. No schema, no API shape, no persisted state, no frontend contract changes. The change is a
statement-ordering swap inside one private helper plus tests and documentation.

## Open Questions

None.
