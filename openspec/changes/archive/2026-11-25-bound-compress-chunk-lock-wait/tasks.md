# Tasks

Fixture level: `expanded`. Risk packs: production database maintenance (the
runner mutates the production hypertables every day); lock behaviour (a new
session setting that decides how long readers can be stalled); receipt
contract (a schema version bump with historical receipts that must keep
validating); configuration fail-closed chain (a new key next to keys whose
absence or inconsistency already refuses a tick); alerting (a unit that now
mails on failure).

Must preserve: selection, lag and `per_tick_bound` behaviour; the budget
chain legs and every existing validation; `acquire_compression_fence`, its
`SET` / `RESET` of `lock_timeout` and its tests; the tick-deadline guard;
reconcile before any label of a failed `compress_chunk`; the generic failure
arm and `_safe_failure` (no raw exception text in a receipt); exit code 0
for `clean`, `deferred` and `refused_lock`, non-zero for `partial` and
`failed`; receipts of schema 1.0, 2.0,
2.1 and 2.2 validate exactly as before; the frozen live-evidence contracts;
the unit's `ExecStartPre` preflight before its single `ExecStart`, its
`TimeoutStartSec`, and where its stdout and stderr go; a live env file that
does not declare the new key keeps working; existing tests stay green
unedited except where they pin the literal `"2.2"` as the runner's current
version or the exact key set of a current receipt (those edits are listed
in the report).

## 1. Runner (`scripts/node27_timeseries_compression.py`)

- [x] 1.1 `NODE27_TIMESERIES_COMPRESSION_LOCK_TIMEOUT_MS`: parsed with the
  fence wait's helper, default 5000 in code, positive integer, strictly
  less than `compress_timeout_ms - fence_wait_ms`; a violation takes the
  existing config-error path before any connection. The value is a field of
  the config object.
- [x] 1.2 `_default_compress_chunk` takes `lock_timeout_ms` (keyword-only,
  no default), issues `SET lock_timeout` after the fence is held and after
  the second `SET statement_timeout`, and turns an error with `pgcode ==
  "55P03"` raised by the compress statement, and only by it, into
  `RelationLockContended` (defined in the runner, not a subclass of
  `FenceContended`). `main()` binds the value with the existing
  `functools.partial`. The stale comments named in `design.md` are
  rewritten.
- [x] 1.3 Tick loop: the `RelationLockContended` handling of `design.md`
  (reconcile first; three outcomes; the not-compressed case is a deferral
  with `contended_on = "relation_lock"`, counted, without error and without
  poisoning `after_bytes`), as a branch of the generic arm after its
  reconcile. The fence arm and the tick-deadline branch add
  `contended_on` (`fence`, `tick_deadline`).
- [x] 1.4 Receipt: `SCHEMA_VERSION = "2.3"`, `budget.lock_timeout_ms`.

## 2. Schema, template, unit

- [x] 2.1 `schemas/timeseries_compression_receipt.schema.json`: version
  2.3 with the rules of `design.md`, touching every version-naming place
  listed there; 2.2 and older unchanged in what they accept and reject. The
  example receipt is a valid 2.3 receipt.
- [x] 2.2 `infra/env/node27-timeseries-compression.example` declares the
  key with its default and a comment (what it bounds; why it is not the
  fence wait). Preflight and supervisor accept an env with and without it.
- [x] 2.3 `infra/systemd/nhms-node27-timeseries-compression.service`:
  `OnFailure=nhms-node27-unit-failure-alert@%n.service` in `[Unit]`, nothing
  else changed; the alert template's consumer comment corrected.

## 3. Spec and documents (orchestrator)

- [x] 3.1 Spec delta `hypertable-compression`: two requirements added, the
  receipt requirement modified for 2.3.
- [x] 3.2 `docs/runbooks/tier-node27-timeseries-storage.md`: the section on
  the fence and the tick deadline gains the lock timeout (what it bounds,
  the 2026-11-19 finding, how a `relation_lock` deferral reads in a
  receipt, what to do when it repeats); the install section and the
  `OnFailure=` recipe cover the compression unit and say that its stderr is
  in the stderr file, not in the journal.

## 4. Tests

Unit tests extend `tests/test_node27_timeseries_compression.py` (fakes, no
database) unless a neighbouring file already owns the subject.

- [x] 4.1 Config: absent key gives 5000; a declared value is used;
  non-numeric, zero, negative, and a value equal to or above
  `compress_timeout_ms - fence_wait_ms` each end with the config error and
  no connection attempt (the connect fake is never called).
- [x] 4.2 Session order, with a recording fake connection: the executed
  statements are, in order, the first `SET statement_timeout`, the fence's
  statements, the second `SET statement_timeout`, `SET lock_timeout = <the
  configured value>`, the compress statement. `main()` passes the configured
  value (the existing propagation test's pattern).
- [x] 4.3 Translation: `LockNotAvailable` raised by the compress statement
  becomes `RelationLockContended` carrying the fence wait already spent;
  a 55P03 error raised by an earlier statement of the session is not
  translated; `QueryCanceled` from the compress statement propagates
  unchanged; the fence is released and the connection closed in every case.
- [x] 4.4 Loop, relation-lock arm, with a compress fake raising
  `RelationLockContended` for the first of two chunks of the same
  hypertable: reconcile is called for it; with reconcile reporting "not
  compressed" the descriptor is `deferred_contended` / `relation_lock` with
  no `error`, the count is 1, the second chunk is compressed, that table's
  `after_bytes` in the totals is a number and not null (the deferral did
  not poison it), the outcome is `deferred`, and `main` exits 0. With reconcile
  reporting "compressed": `committed`. With reconcile raising:
  `indeterminate`, outcome `partial`, non-zero exit.
- [x] 4.5 `contended_on` on the two existing deferral kinds: a
  `FenceContended` chunk carries `fence`; chunks put off by the tick
  deadline carry `tick_deadline`.
- [x] 4.6 Statement timeout stays a failure: a compress fake raising
  `QueryCanceled` gives `failed_before_mutation` with the class name in the
  error, outcome `partial`, non-zero exit.
- [x] 4.7 Schema: receipts built by the runner in the cases of 4.4-4.6
  validate as 2.3 and carry `budget.lock_timeout_ms`, and so do a
  refused-lock receipt and a non-tombstone failed receipt; a 2.3 receipt without
  `lock_timeout_ms`, a 2.3 deferral without `contended_on`, a
  `contended_on` on a committed chunk, and an unknown `contended_on` value
  are rejected; the stored historical receipts and a 2.2 receipt keep
  validating; a 2.2 receipt with `lock_timeout_ms` or `contended_on`
  injected is rejected. The example validates.
- [x] 4.8 Template and unit: the template declares the key exactly once
  with 5000; the budget preflight accepts an env with the key and one
  without it; the unit file has the `OnFailure=` line in `[Unit]`, and the
  existing pins (preflight before the single `ExecStart`) still hold.
- [x] 4.9 Real database (`integration`, `timescaledb_210`), new file
  `tests/test_timeseries_compression_lock_timeout_integration.py`, reusing
  the hypertable fixtures and the bounded poll helper of the fence
  integration tests: the six steps of `design.md` "Evidence on a real
  database", with a bounded wait at every step (no unbounded join or
  sleep-until) and the blocker ended in a `finally`.

## 5. Evidence

- [x] 5.1 Red first, recorded in `.workplans/3380/red-runs.log`: 4.2 (no
  `SET lock_timeout`), 4.4 (the chunk is `failed_before_mutation`) and 4.7
  fail against the unmodified source.
- [x] 5.2 Mutations, by an exact-replacement script kept under
  `.workplans/3380/`, each run against the named test, the failing test and
  location recorded, restored and compared:
  - M1 `SET lock_timeout` issued before the fence -> 4.2 red;
  - M2 `SET lock_timeout` not issued -> 4.2 red;
  - M3 the translation catches `LockNotAvailable` from the whole session
    -> 4.3 red;
  - M4 the relation-lock arm skips reconcile -> 4.4 red;
  - M5a the relation-lock deferral records an error -> 4.4 red;
  - M5b the relation-lock deferral poisons `after_bytes` -> 4.4 red;
  - M4b `RelationLockContended` made a subclass of `FenceContended` -> 4.4
    red (reconcile not called);
  - M6 `QueryCanceled` is treated as contention -> 4.6 red;
  - M7 the upper-bound validation of the key dropped -> 4.1 red;
  - M8 zero accepted -> 4.1 red;
  - M9 the schema accepts a 2.3 deferral without `contended_on` -> 4.7 red;
  - M10 the schema accepts `lock_timeout_ms` in a 2.2 receipt -> 4.7 red;
  - M11 the tick-deadline deferral without `contended_on` -> 4.5 red;
  - M12 the `OnFailure=` line removed -> 4.8 red.
- [x] 5.3 Local: the runner's test files
  (`tests/test_node27_timeseries_compression*.py`),
  `tests/test_timeseries_compression_fence*.py` without the integration
  opt-in, the schema/example validation tests, the unit-pin tests; `uv run
  --no-sync ruff check --no-cache .`; `openspec validate
  bound-compress-chunk-lock-wait --strict --no-interactive`; markdown lint
  of the edited documents; the repository entropy hard gate at its
  baseline.
- [x] 5.4 node-27, temporary detached worktree of the PR head: the unit
  test files of 5.3 without the opt-in; then, through
  `scripts/node27_integration_pg.py run --`, the new integration file and
  `tests/test_timeseries_compression_fence_integration.py` with `-rs` (no
  skip). Receipt on the PR.

Owed on #3380 after merge and deployment, not a merge gate: the node-27
checkout is fast-forwarded away from the 04:25Z tick, the unit file is
installed and `OnFailure` read back, and the next natural tick's receipt is
schema 2.3 with `budget.lock_timeout_ms` 5000 and outcome `clean` or
`deferred`. The second box of the issue's `Verification:` (no uncompressed
chunk older than the newest compressed one) stays open for the selection
slice.
