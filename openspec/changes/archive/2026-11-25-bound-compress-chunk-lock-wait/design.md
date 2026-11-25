# Design

## Today

```text
_default_compress_chunk(database_url, chunk, *, compress_timeout_ms, fence_wait_ms):
    SET statement_timeout = compress_timeout_ms
    acquire_compression_fence(...)        # SET lock_timeout = fence_wait_ms; advisory lock; RESET lock_timeout
    SET statement_timeout = max(1, compress_timeout_ms - fence_wait_elapsed_ms)
    SELECT compress_chunk(chunk)          # no lock_timeout: a relation-lock wait lasts until statement_timeout
```

The tick loop catches `FenceContended` (deferral, nothing copied) and then
`Exception` (reconcile, then `failed_before_mutation` / `indeterminate` /
`committed`). `_safe_failure` writes only the exception class name.

## Change

```text
_default_compress_chunk(..., *, compress_timeout_ms, fence_wait_ms, lock_timeout_ms):
    SET statement_timeout = compress_timeout_ms
    acquire_compression_fence(...)                       # unchanged
    SET statement_timeout = max(1, compress_timeout_ms - fence_wait_elapsed_ms)
    SET lock_timeout = lock_timeout_ms                   # new, after the fence's RESET
    try:
        SELECT compress_chunk(chunk)
    except Exception as error:                           # only this statement
        if getattr(error, "pgcode", None) == "55P03":    # the test the fence uses for its own wait
            raise RelationLockContended(chunk.hypertable_key, fence_wait_elapsed_ms) from None
        raise
```

`RelationLockContended` is defined in the runner. It is not a subclass of
`FenceContended`: the fence arm of the loop comes first and does not
reconcile. It is handled inside the generic `except Exception` arm, which
already holds the one reconcile block: a second `except` clause would copy
that block. The translation tests `pgcode` as the fence does and does not
name `psycopg2.Error`, so a driver stand-in without that class still works.

The setting cannot disturb the rest of the session: a `SET` inside the
transaction is rolled back with it on failure, and after a commit it only
reaches `pg_advisory_unlock`, which never waits.

`lock_timeout_ms` is keyword-only without a default, like the two budgets
beside it, and is bound in `main()` by the same `functools.partial`.

```text
tick loop, per selected chunk:
    except FenceContended:             deferred_contended, contended_on = "fence"            (as today + the field)
    except Exception:                  reconcile (read-only catalog query), as today
        reconcile raises            -> indeterminate, error text as today
        compressed after all        -> committed "...after lost acknowledgement", as today
        not compressed, and the error is RelationLockContended
                                    -> deferred_contended, contended_on = "relation_lock",
                                       fence_wait_elapsed_ms from the exception,
                                       deferred_contended_count += 1, no error, no poison
        not compressed, any other   -> failed_before_mutation, as today
tick deadline reached:                 deferred_contended, contended_on = "tick_deadline"    (as today + the field)
```

The lock timeout only changes the label of the "not compressed" case.

## Configuration

| Key | Default | Validation |
|---|---|---|
| `NODE27_TIMESERIES_COMPRESSION_LOCK_TIMEOUT_MS` | 5000 (in code) | positive integer; strictly less than `compress_timeout_ms - fence_wait_ms` |

Parsed where the fence wait is parsed, with the same helper and the same
config-error path (the runner ends before any connection). The template
declares the key with its default and a comment that says what it bounds
and why it is not the fence wait. The budget preflight, the wrapper and the
supervisor keep no key allowlist (the preflight passes the whole
environment through and cross-checks only its four budget keys), so a live
env file without the key, and one with it, both pass; a test pins that.

`lock_timeout = 0` would mean "wait for ever" to PostgreSQL; the
positive-integer validation excludes it.

## Receipt (schema 2.3)

- `SCHEMA_VERSION = "2.3"`.
- `budget.lock_timeout_ms`: required in 2.3, forbidden before.
- `contended_on`: required on a chunk descriptor whose `mutation_state` is
  `deferred_contended` in 2.3, forbidden on any other descriptor and in any
  earlier version; enum `fence`, `relation_lock`, `tick_deadline`.
- Everything the schema says about 2.2 also holds for 2.3 (budget
  all-or-nothing, config tombstone, `head_sha`, `deferred_contended_count`,
  outcome `deferred`).
- The example receipt moves to 2.3. Receipts of 1.0, 2.0, 2.1 and 2.2 kept
  as fixtures or produced by tests keep validating unchanged.
- The refused-lock receipt and the non-tombstone failed receipt carry a
  `budget` too, so they carry `lock_timeout_ms` in 2.3.

Every place of the schema that names versions is touched deliberately: the
top-level `schema_version` enum; the `["2.1", "2.2"]` budget rule (2.3
joins it); the `const "2.2"` rule (becomes 2.2 and 2.3); the two
`["2.0", "2.1", "2.2"]` enums; a new rule for 1.0 through 2.2 that forbids
`budget.lock_timeout_ms` and `contended_on` on `selected` items (the
existing rule for 1.0 through 2.1 is not edited); a 2.3 rule at item level
(a `deferred_contended` item requires `contended_on`, any other item
forbids it); and the `budget` and selected-descriptor definitions.

Two comments in the runner go stale and are rewritten: the one on the
`CompressChunk` protocol, and the tick-deadline comment that says the
missing `fence_wait_elapsed_ms` is what tells the two deferrals apart.

Consumers: the runner is the only code that names "2.2". The live-evidence
verifier's frozen contracts name 2.0 and stay as they are. Any other reader
of receipts that switches on the version or enumerates descriptor keys is
found by search and listed in the implementation report.

## Unit

`infra/systemd/nhms-node27-timeseries-compression.service` gains, in
`[Unit]`, `OnFailure=nhms-node27-unit-failure-alert@%n.service`. Nothing
else in the unit changes. The alert template's comment that lists its
consumers is corrected. The alert script is checked for any allowlist of
unit names.

Deployment on node-27 is manual (user units are not updated by `git pull`).
The runbook's `OnFailure=` recipe installs the alert template and the unit,
reloads, reads `OnFailure` back and then starts the alert unit once as a
smoke test. For this unit the template is already installed and in use by
the retention and governance units, so only the compression unit is
installed and read back; the smoke start is skipped, because the channel
is live and the start sends real mail.

## Evidence on a real database

Fakes cannot show what TimescaleDB 2.10.2 does. One test file, marked
`integration` and `timescaledb_210`, drives `_default_compress_chunk`
against a throwaway hypertable:

1. a blocker session opens a transaction and reads the chunk (it holds
   `ACCESS SHARE` until it is told to finish);
2. the compress call runs in a thread with `lock_timeout_ms` of 5000; the
   test polls `pg_locks` (with the file's bounded poll helper) until the
   compress backend has an ungranted `AccessExclusiveLock` on the chunk.
   That shows the waiting lock is the one at the end of the transaction,
   and that the locks taken at the start do not conflict with a reader;
3. only then a reader is started; the test sees it waiting in `pg_locks`
   before the timeout fires;
4. the compress call raises `RelationLockContended`; the reader then
   returns, well inside its own statement timeout;
5. the chunk is still uncompressed (`timescaledb_information.chunks`);
6. after the blocker ends, the same call compresses the chunk.

On node-27 it runs through `scripts/node27_integration_pg.py run --`
(#3376). Elsewhere it skips or runs exactly as
`tests/test_timeseries_compression_fence_integration.py` does.

## Alternatives rejected

- **Reuse `fence_wait_ms` as the lock timeout.** No new key, but a reader
  stall of up to fifteen minutes per attempt (D1).
- **Treat the lock timeout as a failure.** It would mail on every contended
  tick once `OnFailure=` exists, for an event that needs no action unless
  it repeats.
- **Take the chunk lock up front** (an explicit `LOCK TABLE ... NOWAIT`
  before `compress_chunk`) to avoid throwing the rewrite away. It would hold
  `AccessExclusiveLock` for the whole rewrite, blocking every reader of the
  chunk for minutes on every tick: the opposite of the goal.
- **`log_lock_waits` / killing the blocker from the runner.** Changing the
  production instance's logging, or cancelling other sessions, is not the
  runner's business.
