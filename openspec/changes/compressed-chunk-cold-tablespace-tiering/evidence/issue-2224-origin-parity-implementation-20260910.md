# Issue #2224 origin-qualified parity implementation evidence (2026-09-10)

## Scope and evidence boundary

- Parent rollout: #1895; Epic: #1891.
- Branch: `feat/issue-2224-origin-chunk-parity`.
- Base master SHA: `a854b137c0e70b7ace2358b432f4c81a8c7d92a9`.
- Implementation plus oracle-fix SHA:
  `27d4faeab9718f0a0f343d392f04871ed1d46808`.
- Effective fixture and repair intensity: `high`.

This evidence proves the child repair in an isolated disposable PostgreSQL 15.2 /
TimescaleDB 2.10.2 cluster. It is not a production G1 PASS. The failed production
G1 at `a8db554d6402bec642e9a05627eae64b2b79aec3` remains a NO-GO, and its absent
census, policy and baseline remain unusable. Production G1 may be retried only
after #2224 merges and #1895 starts again from fresh G0 at the new merged SHA.
Tasks 4.0A and 4.1-4.8 therefore remain unchecked.

## Implemented invariant

Every production parity call now receives the selected `CatalogChunk` and builds
one database-side aggregate over its exact quoted durable origin relation. The
parent hypertable supplies only the validated physical-order business-column
inventory. The aggregate retains the half-open `[range_start, range_end)` fence,
row count, per-column non-null counts, XOR checksum and numeric checksum sum.

The same aggregate statement binds the quoted relation back to the expected
origin OID through an equality against the expected OID cast to `oid`; only exact
boolean `true` is accepted. Missing, blank, partial, non-positive, boolean, parent-alias,
compressed-sibling-alias, OID-drift, name-drift, window-drift and disappeared
origin identities fail closed. Census, runtime preflight and locked revalidation,
post-movement readback and reconciliation, and post-target observation all call
the same owner. The census `3600000` ms and runtime `3600s` statement ceilings
are unchanged.

The isolated discriminator uses the exact durable origin query and rejects plans
that contain the parent hypertable, another origin chunk, another compressed
sibling, an unknown relation, or no selected relation. It permits only the
selected durable origin and its transparent-decompression compressed sibling.
It also keeps direct-origin production validity independent of the optional
`ONLY` observation.

## Local verification at the merged-base candidate

At SHA `7e19331cf9e17f90a89f3458182e893ab7e6aa98`, after merging the current master,
the production selector evaluated the 24 changed paths to 48 unique test files.
The set included all three #2224 focused partitions, the runtime integration
suite, the Docker marker contract, all four performance suites, the runbook
contract, and master’s supplemental river-segment write-surface scan.

Results:

```text
selector and focused guards: 749 passed
shipping-selected suite:     4498 passed, 4 skipped
full collection:              19601 tests collected
full default pytest:          19322 passed, 279 skipped, 1 warning
OpenSpec strict:              PASS
uv run ruff check .:          PASS
changed Python py_compile:    PASS
git diff --check:             PASS
```

The sole full-suite warning was the pre-existing recommendation for ecCodes
2.42.0 or newer while the environment runs 2.41.0. It did not fail the suite.
An attempted lint of every tracked hidden Python file also surfaced a pre-existing
line-length error in `.agents/skills/subagent-workflow/scripts/review_gate.py` on
master; that file is outside this diff, while the repository command
`uv run ruff check .` and changed-file Ruff both passed.

## Disposable oracle failure, diagnosis and repair

The first exact-SHA node-27 disposable run at
`7e19331cf9e17f90a89f3458182e893ab7e6aa98` selected exactly the intended
three-marker test and failed before the data assertions:

```text
1 failed, 1 deselected
AssertionError: plan has no selected relation: set()
```

This was not accepted as evidence. The test cleaned its owned container and work
root; port 55496 was absent afterward. The production checkout remained clean at
`a8db554d6402bec642e9a05627eae64b2b79aec3`, and `nhms-db` remained running.

A read-only diagnosis in another disposable cluster established:

- server version `15.2 (Ubuntu 15.2-1.pgdg22.04+1)`;
- TimescaleDB version `2.10.2`;
- psycopg had already decoded the one-row JSON result correctly;
- default `EXPLAIN (FORMAT JSON)` emitted `Relation Name` but omitted `Schema`
  from the `DecompressChunk` custom scan and its compressed-child sequential
  scan, so the exact `(Schema, Relation Name)` extractor correctly discarded
  both unqualified identities; and
- `EXPLAIN (VERBOSE, FORMAT JSON)` emitted the full identities for the selected
  `_timescaledb_internal` origin and compressed sibling.

The production SQL already named the exact quoted durable origin and required no
change. Commit `27d4faeab9718f0a0f343d392f04871ed1d46808` changed only the test
oracle to request verbose JSON and added a regression carrying the observed
unqualified TimescaleDB 2.10.2 plan shape. Its tests-first proof failed before
the one-line change because the captured statement was
`EXPLAIN (FORMAT JSON) SELECT 1`; afterward the capture required
`EXPLAIN (VERBOSE, FORMAT JSON) SELECT 1`. Exact schema-qualified extraction and
all allowed/forbidden relation checks remain intact; there is no relation-name-only
fallback.

Local verification of that repair:

```text
43 passed, 2 skipped
changed-file Ruff:      PASS
changed-file py_compile: PASS
git diff --check:       PASS
```

## Accepted node-27 isolated result

The rerun used an independent temporary checkout of exact SHA
`27d4faeab9718f0a0f343d392f04871ed1d46808`, the pinned Python 3.11 environment
with `uv run --no-sync`, a randomly owned disposable container/work root, and
loopback port 55496. Database connection environment variables for production and
ordinary integration were unset. The test connects only to the disposable
`postgres` database on 55496.

Command shape:

```text
NHMS_RUN_NODE27_DOCKER=1 uv run --no-sync pytest -q \
  -m 'integration and timescaledb_210 and node27_docker' \
  tests/test_compressed_chunk_cold_runtime_integration.py
```

Result:

```text
oracle_candidate=27d4faeab9718f0a0f343d392f04871ed1d46808
python=3.11.15 candidate_import=PASS
1 passed, 1 deselected in 9.25s
identity_rc=0 pytest_rc=0 cleanup_rc=0
oracle_cleanup=PASS
production_checkout_unchanged=PASS
production_container_running=PASS
```

The executed test proves the production-shaped inventory and exact-origin parity
against compressed chunks, including selected 24-row data, a 7200-row sibling,
an equal-shape checksum discriminator, target-sensitive mutation, structured
schema-qualified plan identity, optional `ONLY` observation, rollback parity,
complete target migration/recompression, changed compressed-sibling identity,
repeat convergence, and a bounded receipt-producing tick.

After both the failed and passing runs, no owned disposable container, work root,
temporary checkout or 55496 listener remained. No production database connection,
census, installer, production relation movement or service/timer change occurred.
No credential, DSN, signed URL or private environment value is included here.
