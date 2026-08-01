# Tasks: fix-retention-refusal-reason-redaction

Fixture level: expanded · Repair intensity: high · Issue #1213

## Risk packs considered (core)

- Public API / CLI / script entry: selected — stderr diagnostic + receipt
  `refusal_reason` are operator-facing output contracts; wire-code prefix
  shapes must survive redaction.
- Auth / permissions / secrets: selected — the whole point: DSN credentials
  must never reach receipt or wrapper log.
- Error handling / rollback / partial outputs: selected — both touched sites
  are failure paths; refused-receipt semantics and exit codes must not change.
- Config / project setup: not selected — no env var/config surface changes.
- File IO / path safety / overwrite: not selected — receipt publication
  mechanics (safe_fs, 0600) untouched.
- Schema / columns / units / field names: not selected — receipt schema field
  set unchanged (refusal_reason stays a string).
- Concurrency / shared state / ordering: not selected — lock/tick flow
  untouched.
- Resource limits / large input / discovery: not selected — no new IO.
- Legacy compatibility / examples: not selected — no legacy callers beyond
  the operator grep contract covered under Public API.
- Release / packaging / dependency compatibility: not selected — no new deps.
  psycopg2 deferral has NO existing regression test (the test module itself
  eager-imports `packages.common.redaction` at top level); this change spreads
  that import to two more call sites, so a new deferral guard is added as
  evidence (see Required evidence).
- Documentation / migration notes: not selected — no doc surface changes;
  #1213 records the wrapper-log-permission observation.

Domain packs (NHMS profile, itemized):
- PostGIS/TimescaleDB domain behavior: not selected — chunk selection SQL,
  drop_chunks invocation, and gate decisions are byte-unchanged; only the
  error TEXT of an already-failing drop is transformed.
- Run manifest / published artifact identity (receipt evidence surface): not
  selected — receipt schema field set and outcome vocabulary unchanged;
  `refusal_reason` stays a string, only its credential content changes.
- Forcing/temporal alignment, geospatial/CRS, numerical/solver, Slurm parity:
  not selected — no such surface touched.

## Implementation tasks

- [x] 1. Generalize the #1212 redaction helper into the module's single
  error-redaction chokepoint (rename or widen docstring; keep function-local
  `packages.common.redaction` import; keep libpq role-name scrub).
- [x] 2. Route the drop-phase reason (:1361-1364) through the helper,
  preserving `RETENTION_DROP_FAILED:<schema>.<chunk>:` prefix.
- [x] 3. Route the uncaught-fallback reason (:1464-1466) through the helper,
  preserving `RETENTION_UNCAUGHT_ERROR:<TypeName>:` prefix.
- [x] 4. Delete dead `_mask_dsn` (:241-252) and the then-unused `urlunsplit`
  import; `git grep "def _mask_dsn" scripts/node27_timeseries_retention.py`
  must return zero hits (a docstring cross-reference to salvage's
  `_mask_dsn_in_message` may remain).
- [x] 5. Upgrade `test_dsn_never_appears_in_stderr` to real driver-shaped
  injection and add drop-phase + receipt-file assertions (see evidence).
- [x] 6. (round-1 verified findings) Make the chokepoint total: wrap the
  helper so it never raises (driver-less host degrades to a credential-free
  placeholder and the refused receipt is still published); extend the role
  scrub to the `role "<dsn-username>"` echo shape; strengthen the deferral
  guard (AST check joins module+alias, plus a blocked-driver subprocess
  import probe).

## Required evidence

- Test: uncaught path, DSN-parse-shaped injection — `fetch_chunks` raises
  `psycopg2.ProgrammingError('invalid dsn: missing "=" after
  "postgresql://alice:supersekret@127.0.0.1:55432/nhms" in connection info
  string')` → exit 1; stderr AND receipt file bytes contain neither
  `supersekret` nor bare `alice`; receipt `refusal_reason` starts
  `RETENTION_UNCAUGHT_ERROR:ProgrammingError:`.
- Test: uncaught path, libpq auth-shaped injection — `fetch_chunks` raises
  `psycopg2.OperationalError('connection to server at "127.0.0.1", port 55432
  failed: FATAL:  password authentication failed for user "alice"')` →
  stderr + receipt clean of `supersekret`/bare `alice`; `user "***"` present
  in refusal_reason (diagnosability retained).
- Test: drop path — seam is `retention.main(argv=[], now=_NOW,
  fetch_chunks=..., measure_chunk_bytes=..., drop_chunk=...)` in enforce mode
  (only `main()` publishes the receipt file; run_retention alone cannot
  evidence receipt bytes). `drop_chunk` raises a DSN/role-bearing driver
  exception → receipt `refusal_reason` starts
  `RETENTION_DROP_FAILED:<hypertable_schema>.<chunk_name>:` (NOTE: this is
  the existing prefix shape from :1362-1363 — hypertable schema + chunk
  name, NOT `ChunkRow.qualified_name` which is chunk_schema-qualified;
  prefix shape zero change), outcome stays `refused`, subsequent chunks are
  NOT attempted (H5 whole-tick fail-closed unchanged), no credentials on
  either stderr or receipt file bytes.
- Test: unchanged sibling — existing measurement-diagnostic redaction tests
  (`_MEASURE_PROBE_DSN` family) stay green unmodified.
- Test: psycopg2/redaction deferral guard — assert the module keeps
  `packages.common.redaction` out of module scope (e.g. static assert that
  the module's top-level source has no `packages.common.redaction` import,
  or a blocked-driver subprocess `--help` probe); rationale: the redaction
  module imports `psycopg2.extensions` at module scope and this runner's
  config parsing/--help must work without the driver.
- Command: `uv run pytest -q tests/test_node27_timeseries_retention.py` all
  green.
- Command: `uv run ruff check .` clean.
- Command: `grep -n '{error}' scripts/node27_timeseries_retention.py` → the
  two refusal_reason construction blocks (drop-phase, uncaught fallback)
  have ZERO raw `{error}` interpolation. Allowed pre-existing hits, not
  touched by this change: `:435` lock-file OSError (`cannot acquire lock
  file: {error}` — OS errno text, no DSN content) and the `{pub_error}`
  receipt-publication sites (`SafeFilesystemError` filesystem paths, no DSN
  content).
- Command: `openspec validate fix-retention-refusal-reason-redaction --strict
  --no-interactive` passes.

## Non-goals

- redaction.py / display_watermark.py behavior changes.
- Wrapper log permission hardening.
- Gate/freed_bytes/receipt-schema changes; no `--now` CLI flag.
- libpq host/port echo (`connection to server at "10.x.x.x", port 55432`)
  is deliberately RETAINED in redacted text — a diagnosability trade-off;
  the redaction contract covers password + DSN string + role-name echo only.
  Host/port-grade residual exposure is accepted and documented here.
