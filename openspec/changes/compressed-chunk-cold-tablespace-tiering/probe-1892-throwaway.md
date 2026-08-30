# Probe log: TimescaleDB 2.10.2 compressed-chunk cold residency (#1892)

**Verdict: PASS.** On PostgreSQL 15.2 / TimescaleDB 2.10.2, the only
accepted complete-group, single-transaction sequence is
`shell_first_decompress_recompress_atomic`:

1. lock and revalidate the compressed origin and sibling heaps in stable OID
   order;
2. move the origin shell and every origin index to the target tablespace;
3. `decompress_chunk(origin)` and prove the expanded origin/index/TOAST group
   is entirely target-resident;
4. `compress_chunk(origin)` and prove the new complete
   origin/compressed/index/TOAST group is target-resident with unchanged
   target-window parity;
5. commit and perform a fresh readback. Rollback proof also uses a fresh
   connection.

`timescaledb_experimental.move_chunk`, direct compressed-heap/TOAST ALTER,
decompress-first, internal compressed-hypertable attach, and a two-transaction
sequence are rejected. Nothing in this probe touched the live `nhms` database,
its container configuration, port, PGDATA, or data paths.

## Environment and isolation

- Oracle host: node-27 (`nwm@210.77.77.27:32099`).
- Live `nhms-db`: read-only image/status inspection only; it remained `running`
  with image ID
  `sha256:ad39c4fbc5c44557db1e16af10ec11e3ab12d0a472374f39aaba06ad9ca2640e`.
  Live port `55432` and live data paths were refused by the probe.
- The live container's `.Config.Image` is the same image-ID digest rather than
  the historical tag literal. The local image still carries RepoTag
  `timescale/timescaledb-ha:pg15-latest` and RepoDigest
  `timescale/timescaledb-ha@sha256:a8e3322e1cf936828698cb4de2a9c4b59acae1b123909f023bb15f42270af95d`;
  the report records `live_ref_alias=digest_image_id` rather than pretending
  `.Config.Image` is the tag.
- Disposable server: `PostgreSQL 15.2 (Ubuntu 15.2-1.pgdg22.04+1)`,
  TimescaleDB `2.10.2`, started from the exact image ID above.
- Disposable resources: independently named `nhms-1892-probe-*` container,
  loopback-only host port, and identity-bound `/tmp/nhms-1892-probe-*` PGDATA
  and tablespace paths. `nhms_cold` resolved to
  `/home/postgres/pgdata/tablespaces/nhms_cold` inside that container.
- Tablespaces are cluster-scoped. A throwaway database inside live `nhms-db`
  is not an isolation boundary and cannot qualify this evidence.

## Reproduction and frozen receipts

Local gates:

```text
uv run pytest -q tests/test_compressed_chunk_cold_residency.py \
  tests/test_probe_compressed_chunk_cold_tablespace.py \
  tests/test_probe_compressed_chunk_cold_tablespace_cleanup.py \
  -m 'not integration'
# 65 passed, 1 deselected

uv run pytest -q tests/test_select_ci_tests.py \
  tests/test_select_ci_tests_probe_cleanup.py
# 398 passed

uv run ruff check .
# All checks passed!

openspec validate compressed-chunk-cold-tablespace-tiering \
  --strict --no-interactive
# Change 'compressed-chunk-cold-tablespace-tiering' is valid
```

Node-27 CLI oracle ran from a fresh temporary GitHub clone at the exact
post-review probe-code commit
`d66d91f52b69051f808eb823cc26bc94f14b7689`. The clone used a detached
checkout, was clean before execution, and did not pull or modify the activity
checkout at `/home/nwm/NWM`:

```text
ROOT=/tmp/nhms-1892-frozen-d66d91f5
git clone --no-checkout https://github.com/DankerMu/SHUD-NWM.git "$ROOT/repo"
git -C "$ROOT/repo" checkout --detach \
  d66d91f52b69051f808eb823cc26bc94f14b7689
test "$(git -C "$ROOT/repo" rev-parse HEAD)" = \
  d66d91f52b69051f808eb823cc26bc94f14b7689
PYTHONPATH="$ROOT/repo" /home/nwm/NWM/.venv/bin/python \
  "$ROOT/repo/scripts/probe_compressed_chunk_cold_tablespace.py" \
  --mode isolated-cluster \
  --host-port 55495 \
  --image-id sha256:ad39c4fbc5c44557db1e16af10ec11e3ab12d0a472374f39aaba06ad9ca2640e \
  --image-ref timescale/timescaledb-ha:pg15-latest \
  --output "$ROOT/cli.json"
# exit 0; status=passed; failures=15
```

Node-27 first ran the frozen cleanup-ownership suite without Docker mutation,
then loaded the frozen repository's real `tests/conftest.py` for the isolated
marker. The dummy URL satisfies opt-in collection but is never connected because
the marker creates its own cluster:

```text
PYTHONPATH="$ROOT/repo" /home/nwm/NWM/.venv/bin/python -m pytest -q \
  "$ROOT/repo/tests/test_probe_compressed_chunk_cold_tablespace_cleanup.py"
# 9 passed in 0.15s

NHMS_RUN_INTEGRATION=1 \
NHMS_INTEGRATION_DATABASE_URL=postgresql://unused:unused@127.0.0.1:1/postgres \
PYTHONPATH="$ROOT/repo" /home/nwm/NWM/.venv/bin/python -m pytest -q \
  "$ROOT/repo/tests/test_probe_compressed_chunk_cold_tablespace.py" \
  -m timescaledb_210
# 1 passed, 25 deselected in 17.22s
```

The two complete downloaded evidence copies passed the checked-in
`parse_probe_report` semantic gate before the identity-bound remote temporary
root was removed:

| Receipt | SHA-256 | Bytes | Result |
|---|---:|---:|---|
| frozen CLI | `a33b5be6f6f5f1906036bc6ba1a2a532c773a4e5be145e7c251cd405bf4600ac` | 287002 | `passed` |
| frozen pytest marker | `54afcb38871cc4b9ec75a70ef8b199cfd66bac03db1e4139c9dc27ab2715e6fe` | 287001 | `passed` |

These full JSON files are local workflow evidence, not committed long-term
fixtures. This log retains every decision-bearing value and the checked-in
probe makes the full report reproducible.

## Eligibility and source group

The probe used business watermark `2026-07-09T00:00:00Z`, lag `604800`
seconds, and exact cutoff `2026-07-02T00:00:00Z`. The hydro chunk ending exactly
at the cutoff was eligible. A separate nonempty failure chunk covered
`[2026-06-18T00:00:00Z, 2026-06-25T00:00:00Z)` and had target-window parity:

```text
count=24
value_sum=138.0
checksum=db2b7ba9b432238a64cc1b09a7d55f53
```

The disposable probe fixture has exactly four business columns (`id`, canonical
UTC `valid_time`, `value`, and NULL-distinct `payload`). #1892 hashes every
fixture column inside the origin chunk's half-open range; that helper is
probe-support only and is not a production-column contract. #1893 must still
cover every live business column on both production hypertables after deriving
that inventory from the production schema. A sentinel changed one target-window
value without changing the aggregate count, then made a compensating change in a
sibling chunk. The target checksum changed from
`44e88875287a81d598d28044dc7e605e` to `001197a9aad128bdc6dd20053404c238` and
the sibling change could not hide it; restoration returned the original checksum.

For the accepted/rollback chunk, the compressed source group was:

| Member class | Representative identity | Source bytes / residency |
|---|---|---:|
| origin heap | `_hyper_1_2_chunk` OID 20828 | 0, `pg_default` |
| origin TOAST + index | OIDs 20831 / 20832 | 0 / 8192, `pg_default` |
| three origin indexes | OIDs 20834 / 20836 / 20837 | 8192 each, `pg_default` |
| compressed heap | `compress_hyper_2_13_chunk` OID 21008 | 8192, `pg_default` |
| compressed TOAST + index | OIDs 21011 / 21012 | 16384 each, `pg_default` |
| compressed index | OID 21013 | 16384, `pg_default` |

Total source group bytes were 90112. The zero-byte origin shell alone therefore
cannot prove that compressed bytes moved.

## Candidate sequence verdicts

| Candidate | Verdict | Measured behavior |
|---|---|---|
| `timescaledb_experimental.move_chunk` | REJECT | `FeatureNotSupported: function must be run on the access node only`; it is also not a single-transaction primitive here |
| direct compressed-heap ALTER | REJECT | `FeatureNotSupported: changing tablespace of compressed chunk is not supported` |
| direct TOAST ALTER | REJECT | `InsufficientPrivilege: ... is a system catalog` |
| decompress-first | REJECT | transactionally reversible, but expands the origin on `pg_default` before moving and is not the production order |
| attach `nhms_cold` to the internal compressed hypertable | REJECT | attach/detach calls worked, business hypertables stayed unattached, but a newly compressed group still resolved `all_source` |
| two transactions | REJECT | physical end state could be reached, but transaction 1 committed an uncompressed/mixed window; `atomic=false` |
| shell-first decompress/recompress | ACCEPT | complete target group before commit, target-window parity unchanged, fresh rollback restored original sibling and source residency |

No business hypertable had `nhms_cold` attached, and a newly created normal
business chunk stayed in `pg_default`.

## Accepted transaction and rollback proof

The probe executed this order with finite local timeouts (the measured `2s` /
`30s` values are tiny-fixture probe settings, **not production defaults**):

```text
BEGIN
SET LOCAL lock_timeout = '2s'
SET LOCAL statement_timeout = '30s'
LOCK TABLE <origin heap> IN ACCESS EXCLUSIVE MODE
LOCK TABLE <compressed heap> IN ACCESS EXCLUSIVE MODE
ALTER TABLE <origin heap> SET TABLESPACE nhms_cold
ALTER INDEX <each origin index in OID order> SET TABLESPACE nhms_cold
SELECT decompress_chunk(<origin>::regclass)
-- resolve + prove expanded origin/index/TOAST all cold
SELECT compress_chunk(<origin>::regclass)
-- resolve + prove new complete group all cold + target-window parity
COMMIT or ROLLBACK
-- fresh observer reconciliation
```

Measured stages:

| Stage | Residency | `nhms_cold` bytes | `pg_default` bytes | Compressed sibling |
|---|---|---:|---:|---|
| before | `all_source` | 0 | 90112 | OID 21008, `compress_hyper_2_13_chunk` |
| after shell/index moves | transient `mixed` | 73728 | 16384 | old sibling; only its compressed index remained hot |
| after decompress | `already_target` | 65536 | 0 | none; expanded origin bytes were entirely cold |
| after recompress | `already_target` | 90112 | 0 | new sibling OID 21441 in rollback probe |
| fresh after rollback | `all_source` | 0 | 90112 | original OID 21008/name restored |

Rollback parity remained exactly `count=24`, `value_sum=138.0`, checksum
`44e88875287a81d598d28044dc7e605e`; reconciliation was
`complete_source`, and `original_sibling=true`. The post-review PASS gate also
required the recorded expanded proof to bind all six uncompressed
origin/index/TOAST members to target `nhms_cold`, with
`all_requested_target=true` and `pg_default_bytes=0`; missing, mixed, or
self-contradictory intermediate evidence fails the report even when the final
recompressed group is cold. The inverse move carries the same target-aware
proof for `pg_default` rather than reusing a cold-only classifier.

A committed repetition created new sibling OID 22065
`compress_hyper_2_30_chunk`, resolved the entire group at `nhms_cold`, and
returned `complete_target` with the same parity. A replacement compressed
sibling at source is classified `unknown`; only the original sibling can prove
rollback.

## Capacity and WAL contract

`chunk_compression_stats` reported
`before_compression_total_bytes=65536` for the nonempty failure chunk. The
source group retained until commit was 90112 bytes. Capacity preflight uses
only explicit inputs:

```text
required_cold = before_compression_total_bytes + operator cold reserve
required_hot = operator WAL reserve
```

Retained source bytes are recorded but do not inflate free space or get counted
as pre-commit reclamation. The probe used explicit one-byte reserves solely to
exercise exact boundary arithmetic; these are **not production values**.
Issue #1893 owns operator-configured production reserves and full-rewrite timeouts.

| Case | Decision | Headroom | Mutation proof |
|---|---|---:|---|
| measured free space | approved | cold 23494750207; hot 23494815743 | normal probe may proceed |
| exact equality | approved | cold 0; hot 0 | boundary is inclusive |
| cold one byte short | refused | cold -1 | `shell_sql_executed=false`; OIDs/residency/sibling/parity unchanged |
| hot one byte short | refused | hot -1 | `shell_sql_executed=false`; OIDs/residency/sibling/parity unchanged |

A separate 1 MiB tmpfs produced genuine `DiskFull: No space left on device`
after preflight; fresh reconciliation proved `complete_source` with original
sibling and unchanged parity. This is the rollback defense, not a substitute
for preflight.

The frozen committed tiny-fixture move advanced the instance LSN from
`0/2264990` to `0/228D0F8` (165736 bytes by subtraction). The report
intentionally labels WAL as instance-level `pg_wal_lsn_diff` from `0/0`, not
per-group WAL attribution; production WAL reserve cannot be derived from this
number.

## Lifecycle proof

- Committed cold move: source sibling OID 21008 -> target sibling OID 22065;
  complete group `already_target`; target-window parity unchanged.
- Re-run: `already_cold`, no rewrite.
- Cold decompression: `is_compressed=false`, all origin/index/TOAST members cold,
  65536 bytes cold / 0 bytes hot.
- Replay: inserted one target-window row; count 24 -> 25 and checksum changed to
  `243da337af54d8cfddaafde44f6e409a`.
- Recompression: new sibling OID 22153; complete group remained cold.
- Inverse shell-first move to `pg_default`: new sibling OID 22250; complete
  group `all_source`, parity preserved at count 25.
- `drop_chunks`: all pre-drop OIDs
  `[20828, 20831, 20832, 20834, 20836, 20837, 22250, 22253, 22254, 22255]`
  were absent afterwards; no origin/compressed/index/TOAST member remained.

## Boundary proof

- Exact cutoff: eligible; newer, other-hypertable, and missing-watermark inputs
  were ineligible/refused as specified.
- Empty chunk: complete origin/compressed/index/TOAST enumeration succeeded.
- True no-origin-index chunk: dedicated hypertable created with
  `create_default_indexes => false`; `no_index_origin_index_count=0` while
  engine-owned compressed storage remained enumerable.
- Multiple/quoted indexes: numeric-leading origin index present and safely
  quoted.
- Owned TOAST: present and resolved through parent ownership; never directly
  ALTERed.
- Same-window hydro/met groups: member OID sets were disjoint.
- `attach_tablespace=[]`; a new ordinary chunk used `pg_default`.

## Failure and concurrency proof

Every rollback row used a fresh observer and the nonempty failure chunk unless
noted. Source recovery required original compressed sibling identity and exact
window parity.

| Injection | Measured result | Fresh terminal proof |
|---|---|---|
| missing target | `UndefinedObject: tablespace ... does not exist` | `complete_source`, original sibling |
| after shell move | injected `UndefinedTable` | rollback `complete_source` |
| after decompress | injected `UndefinedTable` | rollback `complete_source` |
| after recompress | injected `UndefinedTable` | rollback `complete_source` |
| statement timeout | `QueryCanceled` | `complete_source`, original sibling |
| lock conflict | `LockNotAvailable` | zero move, `complete_source` |
| backend termination before commit | connection closed after mutation began | `complete_source`, original sibling |
| lost commit acknowledgement | underlying commit completed; moving-connection commit API returned `CommitAckLost` and the connection became unusable | fresh `complete_target`, new sibling, parity equal, `replayed=false`; classification is from the fresh observer, never a clean-commit relabel |
| insufficient role | `InsufficientPrivilege: must be owner ...` on an `nhms_cold` shell-move statement | zero move, `complete_source` |
| target ENOSPC | the shell-move `ALTER TABLE ... SET TABLESPACE probe_full` itself returned genuine `DiskFull` (filler-only failure is not accepted) | rollback `complete_source` |
| catalog/path mismatch | deliberately wrong expected path | refused before SQL; OIDs/residency/sibling/parity unchanged |
| injected missing-relation SQL | `UndefinedTable` | explicitly **not** claimed as selected-relation disappearance |
| selected relation disappears | dedicated selected chunk dropped from another connection before lock/revalidation | stale plan blocked `origin heap is missing`; every sacrificed OID absent; unrelated witness unchanged |

No failure row produced false success.

## Cleanup and limits

- Both final runs reported `created_container=true`, `container_removed=true`,
  `container_absent=true`, `work_root_absent=true`, and `identity_bound=true`.
- The frozen cleanup suite proved the ownership complement: a failed
  same-name `docker run` leaves `created_container=false`, and terminal cleanup
  issues no `docker rm -f`; `--keep` and unowned-identity refusal remain no-op.
- Final node-27 checks found no `nhms-1892-*` container or work root. The live
  `nhms-db` remained running on the same image ID.
- The node-27 temporary source clones and JSON files were removed after receipt
  download. The activity checkout and its pre-existing untracked evidence were
  untouched.
- Probe timeouts, byte sizes, and one-byte reserves are disposable-fixture
  measurements. #1893 must expose and validate production-scale bounds; #1894
  owns live device/RAID/SMART/backup installation gates; #1895 owns live
  migration and performance evidence.
