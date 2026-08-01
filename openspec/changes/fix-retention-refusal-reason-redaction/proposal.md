# Fix retention refusal_reason: redact driver exception text (#1213)

## Why

Issue #1213. The retention runner (`scripts/node27_timeseries_retention.py`)
interpolates raw driver exception text into `refusal_reason` at two sites
(drop-phase failure `RETENTION_DROP_FAILED:...: {error}` and the uncaught
fallback `RETENTION_UNCAUGHT_ERROR:{type}: {error}`), which persists to TWO
surfaces: the receipt file (0600) and the long-lived wrapper log
`retention.log` (default umask, 0644). psycopg2 echoes the full conninfo
(including plaintext password) on DSN parse failures, and libpq echoes
host/port/role on connection/auth failures. Today password-grade leakage is
*accidentally* blocked by the `fetch_display_watermark` wrapper being the
first DB touch; host/port/role-grade leakage is reachable now (DB restart
between watermark and fetch_chunks; credential rotation mid-run).

The module's own invariant lock `test_dsn_never_appears_in_stderr` injects a
DSN-free `RuntimeError("oops")` — its assertions are vacuously true against
the only dangerous exception class. And the module carries a dead
`_mask_dsn` helper (docstring claims safety, zero call sites).

## What Changes

- Generalize the existing `_redact_measure_error(error, dsn)` (landed in
  PR #1212, currently wired only to the measurement stderr diagnostic) into
  the single redaction chokepoint for BOTH refusal_reason construction
  sites, so receipt and stderr outputs are covered.
- Delete the dead `_mask_dsn` (its capability is covered by
  `packages/common/redaction.redact_database_dsn`); drop the then-unused
  `urlunsplit` import.
- Replace the vacuous DSN test with real driver-shaped injections
  (full-conninfo `ProgrammingError`, libpq `password authentication failed
  for user "..."`) asserting on BOTH stderr and receipt file content, for
  both the uncaught-fallback and drop-phase paths.

## Non-goals

- No change to `packages/common/redaction.py` policy semantics (reuse only).
- No change to `packages/common/display_watermark.py`.
- No `--now`/reference-time CLI override.
- No change to gate decisions, freed_bytes semantics, or receipt schema
  field set.
- Wrapper log file permission hardening (`node27_timeseries_retention_once.sh`)
  is recorded in #1213 as out of scope, not fixed here.
