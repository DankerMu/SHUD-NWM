# Design

## Risk triage

- Fixture level: **compact**. Two SQL statements in one test helper, plus tests.
  No production path, no schema change, no new dependency.
- Divergence from the issues' `预估规模: XS`: none. Two issues are delivered in
  one change because they are two statements in one function; #1654 says so
  explicitly ("与 #1640 同函数同一次提交一起改最省事").
- Risk packs selected: **test-infrastructure correctness** only. The change
  edits teardown, so the failure mode to guard against is a teardown that
  silently stops deleting rows it used to delete, leaking state into the next
  test on a session-scoped database.
- Risk packs not selected, with reason: oracle integrity — nothing here weakens
  a detector or an assertion, and the two existing static oracles over this
  function are preserved rather than relaxed (D4); geospatial/CRS, hydro-met
  windows, SHUD numerics, Slurm lifecycle, provider snapshots, display identity,
  run-manifest provenance — untouched by a two-statement teardown edit.

## Must-preserve behavior

- `_clear_issue_126_rows` still removes every row it removes today. The bound
  must cover the full range actually present under the identity predicate, not
  a range assumed from fixture knowledge (D2).
- The `hydro.river_timeseries` DELETE keeps the literal substring
  `WHERE run_key IN (` and gains no text-identity column —
  `tests/test_river_ts_text_identity_cleanup.py:1110-1119` asserts both by AST
  extraction of the SQL constant.
- Statement order in the function is unchanged. The `river_timeseries` DELETE
  resolves `run_key` through a subquery over `hydro.hydro_run`, whose rows are
  deleted on the **next** statement; any probe added for the bound sits before
  that deletion and depends on the same ordering.
- The other seventeen `cursor.execute` call sites in the function are untouched
  (19 total, minus the two hypertable DELETEs). The function runs `:412-465`.

## Seams under test

- `_clear_issue_126_rows(connection)` driven with a recording fake cursor: the
  exact sequence of `execute` calls and their parameters, for both the
  rows-present and the no-rows paths.
- The SQL constants in the function, read statically, as the two existing
  oracles already do.
- The real teardown on node-27, as the integration-level receipt.

## Decisions

### D1 — Probe the present rows, do not reuse the guard

The shape, applied to both statements and mirroring
`packages/common/forcing_domain_handoff_apply.py:744-797` minus the parts that
only make sense with an incoming batch:

1. Read `min(valid_time)`, `max(valid_time)` under the statement's own identity
   predicate — **one** statement, not two.
2. If `min` came back `NULL`, no row matched: skip the DELETE entirely.
3. Otherwise issue the DELETE with `valid_time >= %s AND valid_time <= %s`
   appended to that same identity predicate.

The production idiom this mirrors uses a separate existence probe *before* the
min/max read. That is deliberately dropped here. There it earns its keep because
the caller unions the existing window with an incoming batch and must tell "no
existing rows" apart from "existing rows outside the batch"; teardown has no
incoming batch, and `min(valid_time)` over zero rows already returns `NULL`,
which is the same signal. One statement instead of two, one fewer string
constant to register (D4), and no behavior lost.

`check_batch_targets_uncompressed` is deliberately **not** called. The argument
is checkable, not a matter of taste: the guard does not change whether the
DELETE succeeds — if a chunk in the window is compressed, the DELETE fails with
or without it — it only changes which error is raised. On the production path
that matters, because the caller must distinguish a guard violation from a write
error and keep going. In teardown, either failure is a test failure and pytest's
traceback already points at the statement. Both issues reach this conclusion
independently (#1654: "teardown 场景无 incoming 批，守卫调用属于多余开销";
#1640 lists the guard idiom only as its 备选). This change consumes that
upstream adjudication rather than reopening it.

There is no race between the probe and the DELETE to reason about. Teardown runs
sequentially on a single connection and a single cursor, with no concurrent
writer, so the probed window cannot go stale before the DELETE executes. Recorded
because the production idiom this mirrors *does* have that concern, and a
reviewer importing production semantics into teardown would otherwise flag it.

### D2 — Do not hard-code the seeded `valid_time` constants

`#1654` offers, as its first recommendation, binding the bound to
`VALID_TIME_1`/`VALID_TIME_2` (`tests/integration_helpers.py:35-36`), since those
are the only two timestamps the fixture writes into `river_timeseries` (four
rows, `:374-386`, all under `FORECAST_RUN_ID`). Verified true today, and
rejected anyway.

Hard-coding leaks the seeder's knowledge into the cleaner. The moment anyone
seeds one more row under `FORECAST_RUN_ID` at a third timestamp — a perfectly
ordinary fixture extension — teardown silently stops deleting it and leaks state
into the next test on the session-scoped database. That is a worse failure than
the one being fixed, because it is silent.

The probe shape has no such coupling, and it is the only shape available for the
second statement anyway (D3), so using it for both also removes an inconsistency
a reviewer would rightly ask about.

### D3 — `met.forcing_station_timeseries` has no seeded window to borrow

`tests/integration_helpers.py` never inserts into that table; the only
`ISSUE_126_PREFIX`-identity rows come from
`tests/test_display_coverage_residual_debt_integration.py:394-418`, on a
per-test throwaway database, and the helper's own docstring (`:330-349`)
explains that seeding stations here is forbidden because
`seed_issue_126_data` runs twice against one session-scoped database while
`_clear_issue_126_rows` never deletes `met_station` / `interp_weight`.

So for this statement there is no fixture constant to bind to even in principle,
and the probe is the only correct source of the bound. This is also why the
no-rows skip path is not a defensive nicety: on the session-scoped database used
by `tests/test_real_database_integration.py`, this table is normally **empty**,
so the skip path is the common case, not the edge case.

### D4 — Three static oracles watch this function, and one of them must be updated

Found in fixture review, which is the cheapest place to find it. There are
**three**, not two, and the first draft of this design named only two and
asserted the wrong thing about them.

`tests/test_river_ts_text_identity_cleanup.py` holds two of the three:

1. `test_integration_helpers_delete_targets_the_run_key` (`:1109-1119`) pulls
   every string constant in `_clear_issue_126_rows` containing
   `hydro.river_timeseries` via AST (`_sql_constants`, `:220-262`), asserts there
   is exactly **one**, and pins its shape: it must contain `WHERE run_key IN (`
   and carry no text-identity predicate.
2. `test_every_registered_file_declares_its_river_timeseries_statement_count`
   (`:1371-1387`) counts every non-docstring string-constant occurrence of
   `hydro.river_timeseries` across the whole file (`_river_table_mentions`,
   `:286-299`) and asserts it equals the number registered in
   `RIVER_TABLE_CENSUS` (`:163-173`) — currently `2`, annotated at `:162` as
   "the dual-write INSERT + the cleanup DELETE".

Adding a min/max probe adds one string constant naming the table, so **both**
numbers move: `1 -> 2` and `2 -> 3`. There is no probe shape that reads the
table's own `valid_time` range without naming the table, and a bound derived
from a correlated subquery inside the DELETE would defeat chunk exclusion and so
fail on compressed chunks anyway — which is the entire point of the fix.

Updating those two numbers is **not** weakening an oracle; it is the maintenance
operation the census was built to force. Its own docstring says so:

> The census makes the ADDITION itself red, so the author has to come here,
> register the statement and give it a shape pin.

So the edit is bounded and it must be a net **gain** in assertions: the new probe
statement gets its own shape pin (same identity predicate, no text-identity
column), and the DELETE's existing pin is kept and selected by content rather
than by list position, since the probe now precedes it. If the edit ever
subtracts an assertion instead of adding one, that is the signal to stop.

The third oracle is `tests/test_timescale_write_guard_wire_site_invariant.py`,
which never sees this file: its scan roots are `workers/`, `packages/common/`,
`scripts/`, `db/` and it excludes `tests/` outright (`:80-97`). So skipping the
guard (D1) cannot trip it, and equally it offers this change no protection —
which is the #1642 gap, not something to fix here.

### D5 — #1654's forward-compatibility claim, verified rather than assumed

`#1654` was written before `#1442` migrated the identity predicate from `run_id`
to `run_key`, and predicted that the missing bound was orthogonal to that
migration and would survive it. Confirmed by reading the current master: the
statement now reads `WHERE run_key IN (SELECT run_key FROM hydro.hydro_run WHERE
run_id IN (%s, %s))` and still carries no `valid_time` predicate. #1654's second
acceptance criterion — that the bound holds regardless of which identity column
is used — is therefore discharged by construction: the bound is appended to
whatever identity predicate the statement carries, and the probe reuses that
same predicate rather than restating it.

### D6 — The red proof must be local, not node-27

node-27 is the terminal oracle for this change, but a two-statement fix cannot
iterate against a remote integration run. The local oracle is a recording fake
cursor driven through `_clear_issue_126_rows`, asserting the emitted `execute`
sequence: bounded DELETE with both bounds present when the probe returns a
window, and **no DELETE statement at all** when the probe returns no rows. The
skip path is an explicit acceptance criterion of #1640 and must be asserted, not
inferred from "node-27 did not blow up".

## Evidence mapping

| Acceptance criterion | Source | Evidence |
|---|---|---|
| `river_timeseries` DELETE carries both bounds | #1654 | recording-cursor test + static SQL assertion |
| bound holds independent of the identity column | #1654 | D5; probe reuses the statement's own predicate |
| `forcing_station_timeseries` DELETE carries a bound | #1640 | recording-cursor test |
| no unbounded DELETE when no rows match | both | recording-cursor test asserting the DELETE is absent |
| node-27 real-DB teardown passes | both | `-m integration` receipt on the two calling modules |
| the statement register still bites after being updated | D4 | census goes red when the probe is removed but the number is left at 3 |

## Non-goals

- Calling `check_batch_targets_uncompressed` from teardown (D1).
- #1642's static enforcement, or widening its scan roots.
- The seventeen other `cursor.execute` call sites in the same function.
- Any production path.
