# Design: fix-retention-refusal-reason-redaction

Fixture level: expanded (mandatory trigger: `credential`/`secret`)
Repair intensity: high (secrets + evidence chain)
Project profile: NHMS (openspec/project-profile.md)

## Change surface

- `scripts/node27_timeseries_retention.py`
  - `_redact_measure_error` (:900-931) — generalize naming/docstring to a
    module-wide error-redaction helper (it already has the right semantics:
    `redact_database_dsn` + `***` marker + libpq role-name scrub bound to the
    DSN's own username; keep the function-local import so psycopg2 stays
    deferred).
  - Drop-phase reason construction (:1361-1364) — route `{error}` through the
    helper; keep the `RETENTION_DROP_FAILED:<hypertable_schema>.<chunk_name>:`
    prefix shape (as coded at :1362-1363 — NOT `ChunkRow.qualified_name`,
    which is chunk_schema-qualified; the existing shape is the contract).
  - Uncaught fallback reason construction (:1464-1466) — route `{error}`
    through the helper; keep the
    `RETENTION_UNCAUGHT_ERROR:<TypeName>:` prefix shape.
  - `_mask_dsn` (:241-252) — delete (dead code; zero call sites); remove the
    then-unused `urlunsplit` import (:68).
- `tests/test_node27_timeseries_retention.py`
  - `test_dsn_never_appears_in_stderr` (:2106-2119) — upgrade to real
    driver-shaped injections; add drop-phase coverage; assert on receipt file
    content as well as stderr.

## Approach

One chokepoint, already proven in-tree: every operator-facing error string in
this module flows through the PR #1212 helper. The helper is renamed (e.g.
`_redact_error_text`) or kept with a widened docstring — implementer's call,
but there must be exactly ONE redaction helper in the module afterwards, with
call sites at: measurement diagnostic (existing), drop-phase reason, uncaught
fallback reason.

Redaction contract (round-1 hardened from the #1212 helper):
- verbatim DSN and password (URL-encoded + decoded forms) → `***`
- libpq role echo shapes `user "<dsn-username>"` / `role "<dsn-username>"`
  → `user "***"` / `role "***"` (bounded to the DSN's own username)
- exception type name and wire-code prefixes remain intact (diagnosability)
- the chokepoint is TOTAL: it never raises; internal failure (e.g. redaction
  module unimportable on a driver-less host) degrades to a credential-free
  placeholder so the refused receipt is always published
- libpq host/port echo is deliberately RETAINED (diagnosability trade-off;
  residual host/port-grade exposure accepted, recorded in tasks.md non-goals)

## Invariant Matrix

Governing invariant: no persisted operator-facing surface produced by the
retention runner (receipt file, stderr/wrapper log) may contain DSN
credentials (password, or the DSN username in libpq role echo form); every
driver/uncaught exception text crosses exactly one redaction chokepoint
before interpolation.

Source-of-truth identity/contract: `config.database_url` (the only secret
this module holds); wire codes `RETENTION_DROP_FAILED` /
`RETENTION_UNCAUGHT_ERROR` prefix shapes.

Surfaces:
- Producers: `_default_fetch_chunks`, `_default_measure_chunk_bytes`,
  `_default_drop_chunk` (raw psycopg2 exceptions); `run_retention` internals
  (any uncaught exception).
- Validators/preflight: the redaction helper (single chokepoint).
- Storage/cache/query: receipt file via `publish_receipt` (0600) — content
  must be clean regardless of file mode; wrapper log via stderr redirect
  (0644, out-of-scope to harden, in-scope to keep clean).
- Public routes/entrypoints: `main()` CLI; `_emit_stderr_diagnostic`.
- Frontend/downstream consumers: operators grepping receipt/retention.log by
  wire code — prefix shapes must not change.
- Failure paths/rollback/stale state: RETENTION_CONFIG_INVALID paths (:1398-1423)
  interpolate `RetentionConfigError` text — config errors are constructed from
  env-var names/values by this module, not driver echo; confirmed no DSN
  interpolation (DATABASE_URL config failure is "must be set", not the value);
  out of scope with this rationale.
- Evidence/audit/readiness: receipt `refusal_reason` retains wire-code prefix
  + chunk name + exception type name so #854-era grep runbooks keep working.

Regression rows:
- uncaught path + `ProgrammingError` carrying full conninfo → receipt
  `refusal_reason` starts `RETENTION_UNCAUGHT_ERROR:ProgrammingError:`, and
  neither receipt file bytes nor stderr contain the password or bare username.
- drop path + libpq auth failure text carrying `user "alice"` → receipt
  `refusal_reason` starts `RETENTION_DROP_FAILED:<hypertable_schema>.<chunk_name>:`
  (existing :1362-1363 shape), outcome `refused`, subsequent chunks NOT
  attempted (H5 fail-closed unchanged), no password, `user "***"`. Seam:
  `retention.main(...)` so the receipt FILE is actually published.
- unchanged sibling: measurement diagnostic path (`_default_measure_chunk_bytes`
  stderr warning) keeps its existing redacted behavior and its existing tests
  stay green.

## Review focus

1. Both reason-construction sites actually route through the helper (no
   remaining raw `{error}` on an operator-facing surface).
2. Receipt FILE content asserted, not just stderr capture.
3. Prefix shapes (`RETENTION_DROP_FAILED:<chunk>:`, `RETENTION_UNCAUGHT_ERROR:<Type>:`)
   unchanged — operator grep contract.
4. `_mask_dsn` fully removed with no dangling imports; helper remains the
   single redaction path.
5. psycopg2 stays deferred (no module-scope import creep via redaction module).
