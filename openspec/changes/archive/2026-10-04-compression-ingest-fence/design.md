# Design — compression-ingest-fence (#2713)

## D0 Facts (pinned or to be measured, all recorded in the PR)

1. **Compressed targets are fixed by code.** They are `hydro.river_timeseries`, `met.forcing_station_timeseries`, and each one's `_legacy` sibling when it exists (`packages/common/node27_timeseries_discovery.py` `CANONICAL_HYPERTABLES` / `LEGACY_HYPERTABLES`). The fence key derives from the chunk's hypertable identity (`chunk.hypertable_key`, or schema+name).

2. **Production writers of those tables.** There are three, by DELETE/INSERT site:
   - `workers/output_parser/parser.py::upsert_river_timeseries`
   - `packages/common/forcing_domain_handoff_apply.py::_apply_with_cursor` → `_replace_forcing_station_timeseries`
   - `workers/forcing_producer/store.py::_replace_values` / `replace_forcing_timeseries`. Confirm whether this runs on node-27 and say so in the PR; fence it either way.
   
   The manual SQL under `scripts/ops/node27_1729_*.sql` and `node27_2621_*.sql` is not fenced. The runbook says an operator must hold the lifecycle flock or stop the compression timer before running them.

3. **Ingest-path timeouts.** These are why ingest must not block.
   - Parser connections inject `statement_timeout = DEFAULT_DB_STATEMENT_TIMEOUT_MS = 60_000`, and production node27-ingest.env does not override it (#2529 receipt).
   - Forcing apply uses autopipe `_connect` with a 600 s timeout.
   - A blocking fence wait would be cancelled with 57014. That marks the run `failed` (`mark_run_failed`) and trips the #2529 residency alert.
   - **So ingest never blocks on the fence. See D3.**

4. **Measure on node-27 (TimescaleDB 2.10.2)** with a throwaway hypertable that has the production FK shape (FK columns in segmentby). Take a `pg_locks` snapshot of a `compress_chunk` pid while another session holds RowExclusive on the FK-referenced table. Record which locks compress_chunk takes on referenced tables and when (copy versus finish).
   - This decides nothing in the design, because D3 puts the fence first in the transaction regardless.
   - It documents why the fence has to come first, and seeds the forcing-shape regression test in D5.

5. **Measure on node-27** the longest single parser write *transaction* during catch-up: probe → … → commit, from the parser logs or `pg_stat_activity` sampling. The compression fence-wait default (D2) must exceed it, with margin. Record the number.

## D1 Fence module, new: `packages/common/timeseries_compression_fence.py`

- **Keyspace.** Use the two-int4 advisory form, `(classid, objid)`. `FENCE_CLASS_ID` is a fixed int4 constant and the objid is derived per canonical hypertable family: a `_legacy` sibling shares its canonical table's key, because DDL on a legacy chunk locks the same FK-referenced tables (`core.river_segment`, `hydro.hydro_run`, `met.met_station`, `met.forcing_version`) that a canonical ingest writer holds. This is disjoint from the existing single-bigint `hashtextextended` advisory keys: the parser QC lock and the interp-weight lock. Hash deterministically in Python; pin the values in a test.
- **`try_ingest_fence(cursor, hypertable) -> bool`** runs `SELECT pg_try_advisory_xact_lock_shared(classid, objid)`. It is non-blocking and transaction-scoped.
  - Under PG lock-queue rules, a new shared request conflicts with a *queued* exclusive request, so it returns false while compression waits for or holds the fence. Compression therefore gets priority, and in-flight writers drain.
  - A backend that already holds the shared lock is granted again.
- **`acquire_compression_fence(cursor, hypertable, wait_ms) -> int`** sets `lock_timeout = wait_ms`, runs `SELECT pg_advisory_lock(classid, objid)`, then resets `lock_timeout`. It returns the elapsed wait in ms, measured client-side, and raises a typed `FenceContended` on SQLSTATE 55P03.
- **`release_compression_fence(cursor, hypertable)`** runs `pg_advisory_unlock`. A session-level advisory lock survives rollback, so the caller rolls back first, then unlocks, and also relies on closing the connection. An unlock failure must never mask the original exception.
- **Module docstring** must say:
  - ingest takes a shared, try-only, xact-scoped lock;
  - compression takes an exclusive, session-scoped, bounded lock while holding no other lock;
  - the fence must be the **first statement** of every writer transaction;
  - why: FK-referenced-table locks taken earlier in the transaction would re-create the #2713 cycle;
  - the operational effect: ingest on that hypertable defers while one chunk compresses.

## D2 Compression runner (`scripts/node27_timeseries_compression.py`)

**Knob.** Add `NODE27_TIMESERIES_COMPRESSION_FENCE_WAIT_MS`.
- Positive int, parsed like the other knobs, with a default justified by D0.5, for example 300000.
- Validated fail-closed before any DB connection: it must be strictly less than `COMPRESS_TIMEOUT_MS`.
- Bound into the compress callable keyword-only via `functools.partial`, with no default, mirroring `compress_timeout_ms`.
- Add it to `infra/env/node27-timeseries-compression.example`; the literal pin is in `tests/test_node27_timeseries_compression.py`. Document it in the runbook.

**Per chunk, one new connection:**
1. `elapsed = acquire_compression_fence(cur, hypertable, fence_wait_ms)`.
2. `SET statement_timeout = compress_timeout_ms - elapsed`, at least 1. Fence wait is charged against the chunk's existing budget, so the leg-1 invariant `ceil(compress_timeout/1000) + cleanup ≤ wrapper wall` is unchanged and no env or systemd change is needed.
3. `SELECT compress_chunk(...)`, commit.
4. `finally`: rollback if needed, release the fence, close.

**FenceContended.**
- It is caught **before** the generic `except Exception` (currently around the per-chunk handler), so it runs no reconcile and does not poison `after_bytes`.
- The chunk gets `mutation_state = "deferred_contended"`, and the runner continues with the next chunk.

**Receipt.**
- `SCHEMA_VERSION = "2.2"`, with the schema `version` enum extended.
- `mutation_state` enum gains `"deferred_contended"`.
- New top-level field `deferred_contended_count`.
- Per-chunk descriptor: `fence_wait_elapsed_ms`.
- The 2.2 `budget` object records the effective `fence_wait_ms`, and the receipt-budget requirement gets a MODIFIED delta that copies every existing scenario.
- The schema's outcome enum list that makes `head_sha` required gains `deferred`.
- New outcome value `"deferred"`: no failure, at least one deferral. `main()` maps `clean` and `deferred` to exit 0, and a real failure stays `partial` with exit 1. Other fields are unchanged.
- Update `schemas/timeseries_compression_receipt.schema.json` and `schemas/examples/timeseries_compression_receipt.example.json`, plus the pins: `"2.1"` in `tests/test_node27_timeseries_compression.py`, the jsonschema validations there, and `tests/test_node27_lifecycle_contract.py`. Live-evidence pins of `"2.0"` stay.

**Spec deltas** in this change: MODIFIED `hypertable-compression` "Compression runner timeout budget chain MUST be operator-configurable and fail closed". The override scenario now says the session statement_timeout equals the compress timeout minus the measured fence wait, and the knob joins the fail-closed validation. Copy every existing scenario. Do not change the other budget requirements unless the implementation needs it, and if it does, add a MODIFIED delta for each.

## D2b Retention runner (`scripts/node27_timeseries_retention.py`), in scope since 2026-10-04 14:36

**Evidence.** The scheduled retention tick failed with `RETENTION_DROP_FAILED:hydro._hyper_9_163_chunk: lock-contention(40P01)`:
- `drop_chunks` pid 15822 was waiting for `AccessExclusiveLock` on `hydro.hydro_run`, the FK-referenced table, blocked by parser pid 15841;
- pid 15841 was waiting for `RowShareLock` on relation 24267, blocked by the drop.

This is the same cycle family as #2713, and it confirms that chunk DDL locks FK-referenced tables.

**Fix.** In `_default_drop_chunk`, before `drop_chunks`, take the same exclusive per-hypertable fence through `acquire_compression_fence`, bounded by the existing `lock_timeout_ms`. Use the same session, holding no other lock. Release it in `finally`.

**On fence timeout,** route through the existing #1664 lock-contention classification as `55P03`. The refusal reason keeps its current `RETENTION_DROP_FAILED:<schema>.<chunk>: lock-contention(55P03): ...` shape, so there is **no receipt or schema change** and the existing refusal semantics and alerting are unchanged.

**Tests.**
- unit: the fence is acquired before the drop and released when the drop raises; fence contention renders as `lock-contention(55P03)`;
- real-DB: a parser-shaped transaction (fence-first, then `hydro_run FOR UPDATE` and an INSERT) running against a drop no longer yields 40P01.

**Must preserve:** retention's gating, its lock bounds and its receipt shape.

## D3 Ingest writers: try-fence as the first statement of the write transaction

- **`parser.py::upsert_river_timeseries`**: before `hydro_run ... FOR UPDATE`.
- **`forcing_domain_handoff_apply.py::_apply_with_cursor`**: before `_refuse_legacy_routed_forcing_version`, which comes before any `forcing_version`/`met_station` write. The caller-managed transaction path, `apply_forcing_domain_handoff(cursor=...)`, cannot guarantee first-statement placement. Document that, and verify that the only production caller, `scripts/node27_autopipeline.py`, uses `connection=`.
- **`store.py::_replace_values`**: before `pre_write_cursor_hook`.
- **When the try fails**, raise a typed `IngestFenceBusy` with its own reason code, for example `OUTPUT_PARSE_COMPRESSION_FENCE_BUSY` and `HANDOFF_APPLY_COMPRESSION_FENCE_BUSY`, and roll back.
  - The parser must **not** call `mark_run_failed`. The run stays in a state the next autopipe tick picks up again; verify the exact status transitions and test them.
  - Autopipe classifies it as non-failing `"skipped"`/deferred, which does not count toward its rc. The precedent is the `REASON_APPLY_LEGACY_STORE_REFUSED` → `"skipped"` mapping.
  - The forcing producer path does the equivalent and retries later.
- **The busy signal must cross process boundaries.** Autopipe runs the parser as a subprocess (`workers.output_parser.cli parse`) and sees only the rc and stderr.
  - **parser** — in `parse_run`, a dedicated `except IngestFenceBusy` placed **before** `except OutputParsingError` / `except Exception`. It does not call `mark_run_failed` and re-raises. `IngestFenceBusy` must not be swallowed as an `OutputParsingError`.
  - **cli** — both the click and the argparse entry print `OUTPUT_PARSE_COMPRESSION_FENCE_BUSY: ...` on stderr and exit non-zero.
  - **autopipe** — `_process_run` maps that stderr code line (via the existing parse-error line regex) to `outcome="skipped"` in the `rc != 0` branch, before the deterministic-parse-error and decline logic.
  - **forcing apply** — `apply_forcing_domain_handoff` gets a dedicated branch before its generic `except Exception` that returns `HANDOFF_APPLY_COMPRESSION_FENCE_BUSY`. Autopipe's apply-reason → `"skipped"` mapping recognises it.
  - **tests** — drive autopipe's classification from CLI-level rc and stderr, not from an in-process exception.
- Nothing else in these functions changes.

## D4 Structural guard against a missed writer

Extend `tests/test_timescale_write_guard_wire_site_invariant.py`, or add a sibling invariant built the same way and derived from the same hypertable set. Every function that DELETEs from or INSERTs into a compressed hypertable must call the ingest fence, and the fence call must precede the function's other DB statements in that transaction. A new, unfenced write site fails the test.

## D5 Tests

**Unit tests** (default lane, offline):
- fence keys are stable and pinned, and distinct per hypertable;
- the knob validates fail-closed, including fence_wait ≥ compress_timeout;
- the statement_timeout passed to compress equals compress_timeout − elapsed;
- `FenceContended` → `deferred_contended` with no reconcile, outcome `deferred` exits 0, mixed with a failure gives `partial` and exit 1;
- the fence is released when compress raises;
- each writer: the try-fence is the first statement (recording fake cursor); busy → `IngestFenceBusy` → no `mark_run_failed`; autopipe classifies it as skipped;
- the schema 2.2 example validates.

**Real-DB tests** (`@pytest.mark.integration` and `@pytest.mark.timescaledb_210`, run on the node-27 oracle and never by CI's `not timescaledb_210` lane). Use a throwaway compressed hypertable with production-like FK segmentby.

1. *River-shape red*, no fence. Session P runs BEGIN and the probe, which takes AccessShare on all chunks. Session C runs compress_chunk on chunk X. Poll `pg_locks` until C's pid shows `AccessExclusiveLock granted=false` on X. Then P runs a DELETE on a **different** chunk window.
   - Assert that exactly one session gets 40P01 and record which one.
   - If the interleaving cannot be reached on 2.10.2 (for example, C never shows a waiting AccessExclusive), record the attempt and the observed `pg_locks` in the PR instead of faking a red.
2. *River-shape green*, fenced on both sides, same interleaving.
   - No 40P01.
   - Either P's try-fence fails (P defers, C proceeds), or C waits for P's commit and then compresses.
   - P's committed rows match a no-compression baseline when P is allowed to finish.
3. *Forcing-shape regression.*
   - Taking a RowExclusive on the FK-referenced table first and the fence afterwards reproduces the cycle, or is documented per D0.4.
   - Fence-first gives no cycle.

## D6 Docs and routing

- **Runbook** `docs/runbooks/tier-node27-timeseries-storage.md` gains a section on the fence:
  - semantics, and the first-statement rule;
  - ingest defers during a chunk compress, roughly 20–35 min per chunk and at most 2 a day;
  - the knob and how it is charged against the budget;
  - receipt `deferred` / `deferred_contended`;
  - starvation: escalate when `deferred` repeats several days, following the retention escalation rule;
  - manual ops SQL must hold the lifecycle flock or stop the timer.
- **Docstring**: `packages/common/node27_timeseries_lifecycle_lock.py` lines 4–5 say autopipe stays outside the flock and is now ordered against compression by the DB advisory fence.
- **Selector**: `scripts/select_ci_tests.py` gets a `PATH_TEST_RULES` entry for the new module, routing to the fence tests and the compression, parser, forcing-apply and store suites, the same way as the neighbouring rules.
- No hard `file.py:NNN` line references in new comments (the #2648 gate).

## Must preserve
- The lifecycle flock semantics among compression, retention and replay, including refused_lock exiting 0.
- Compression selection, budget preflight and the leg-1/leg-2 invariants as written.
- The `compress_timeout_ms` keyword-only binding contract.
- The parser's transactional replace semantics: same rows, one transaction.
- The run-status machine, apart from the new non-failing busy path.
- No new role privileges.

## Out of scope
- The unattributed rc=124 on 10-01.
- The compression unit's missing `OnFailure=`, which gets a separate issue.
- #2690.
