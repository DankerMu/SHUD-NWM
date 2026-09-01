# Issue #1894 disposable install, recovery and governance evidence

## Frozen oracle identity

- Issue: #1894
- PR: #1930
- Branch: `feat/issue-1894-cold-tablespace-install`
- Exact tested code commit:
  `56de13a154347a351e5848a0e5922c87999ac29b`
- Oracle checkout: `/tmp/nhms-issue1894-oracle-56de13a1` on node-27
- Runtime: Python 3.11.15, PostgreSQL 15.2, TimescaleDB 2.10.2
- Pinned image:
  `sha256:ad39c4fbc5c44557db1e16af10ec11e3ab12d0a472374f39aaba06ad9ca2640e`
- Isolation: synthetic `nhms-1894-tablespace-*` names, ephemeral non-55432
  ports and owned `/tmp/nhms-1894-tablespace-*` roots only. Production
  `nhms-db`, port 55432, `/home/nwm/NWM`, `/home/nwm/nhms-pgdata` and
  `/data/GHDC` were integrity or read-only observation subjects, never mutation
  targets.

The evidence-only commit that carries this document may follow the tested code
commit. It does not change installer/governance source, schemas or tests; any
later semantic code change requires a new exact-SHA oracle.

## Checked-in fixture and regression matrix

- RAID and SMART:
  - healthy `[UU]`, degraded, rebuilding, recovering/reshaping,
    missing/substituted-member and unknown RAID fixtures;
  - exact two-member SMART PASS, member FAIL/unknown and descriptor-identity
    reuse refusal.
- Path, mount and capacity:
  - correct/wrong/missing mount, absent/nonempty/symlink/wrong owner, mode or
    device path, resident PostgreSQL subtree, capacity and rollback headroom;
  - measured zero is preserved as insufficient capacity, missing remains nullable
    and named unavailable, and negative values reject without clamp.
- Catalog, bind and placement:
  - absent/expected/drifted catalog, dangling current/stopped bind and
    `pg_tblspc` target, no hypertable attach, new chunks in `pg_default`.
- Backup:
  - PGDATA-only refusal and PGDATA plus every external tablespace target.
- Docker recreation:
  - exact bounded inspect normalization, resolved image ID, Docker 28 fields,
    two-segment default-rw bind equivalence, direct argv recreation and
    before/after diff that permits only the cold bind.
- Recovery authority:
  - durable write-ahead pending action for path create, stop, rename,
    replacement create, catalog create/drop, replacement remove, prior
    rename-back/start and owned path remove;
  - exact pre/post adoption, mixed/unknown refusal, no install-DDL replay,
    terminal unlink retry and truthful authority publication.
- Receipts and redaction:
  - dry-run, already-ready, NO-GO, progress, installed, pending-cleanup,
    rollback and error states; mode 0600 publication and secret-free public
    identity.
- Governance:
  - dual-device status and identity, explicit ext4 reserved bytes,
    `total = used + free + reserved`, unavailable/null propagation, strict
    status-conditioned schema, malformed relation-row refusal and observed empty
    inventory as a legitimate zero.
- Selection and markers:
  - explicit producer/consumer routing for pending, recovery/observation and
    governance collection owners; six real nodes carry all three opt-in markers.

## Local verification at the tested code commit

```text
uv run pytest -q
```

- PASS: 15637 passed, 218 skipped, one existing ecCodes version warning.

```text
uv run pytest -q \
  tests/test_node27_cold_tablespace_*.py \
  tests/test_node27_cold_governance.py \
  tests/test_node27_resource_governance.py \
  tests/test_select_ci_tests.py
```

- PASS: 718 passed, 6 skipped.

Additional gates:

- `uv run pytest -q tests/test_select_ci_tests.py`
  - PASS: 417 passed.
- shipping installer/governance schemas against all nine examples
  - PASS; the `healthy + status=ok + identity/bytes=null` mutant is rejected.
- `uv run ruff check .`
  - PASS.
- `openspec validate compressed-chunk-cold-tablespace-tiering --strict --no-interactive`
  - PASS.
- official repository entropy report
  - no #1894 finding; all changed/new #1894 production Python owners are below
    1000 physical lines.

## Node-27 disposable Docker/PostgreSQL/TimescaleDB oracle

Collection at the exact checkout selected six nodes and deselected 33:

```text
NHMS_RUN_NODE27_DOCKER=1 uv run --directory \
  /tmp/nhms-issue1894-oracle-56de13a1 pytest -vv -rs \
  -m "integration and timescaledb_210 and node27_docker" \
  /tmp/nhms-issue1894-oracle-56de13a1/tests/test_node27_cold_tablespace_integration.py
```

Result: **6 passed, 33 deselected in 173.65 seconds**.

- `test_real_disposable_cluster_installs_through_run_install`
  - installed through the production core state machine;
  - final observed snapshot/digest matched;
  - a second invocation was no-write `already_ready`;
  - business hypertables were not attached and new data remained in
    `pg_default`.
- `test_real_post_recreate_failure_rolls_back_only_owned_state`
  - restored the exact prior config/read path and removed only installer-owned
    state.
- `test_real_interrupted_replacement_recovers_without_install_replay[stop]`
  - recovered a stop mutation acknowledged after durable arm and before confirm.
- `test_real_interrupted_replacement_recovers_without_install_replay[rename]`
  - recovered the exact renamed-prior topology without replaying install work.
- `test_real_interrupted_replacement_recovers_without_install_replay[run]`
  - recovered the created replacement using exact inventory and ownership.
- `test_real_terminal_unlink_retry_closes_installed_without_docker_replay`
  - first result was truthful pending-cleanup when authority unlink failed;
  - retry closed installed without stop/rename/run/rm replay.

Wrapper terminal result:

```text
oracle_sha=56de13a154347a351e5848a0e5922c87999ac29b
pytest_rc=0
integrity_rc=0
```

## Production and cleanup integrity

The wrapper compared every integrity subject before and after the six tests:

- live container ID remained
  `93a0eb3586eaec59beb54d665be49d6f9defc1d8138f28af16a10f794c2f5f01`;
- live `Config.Image` and resolved `Image` both remained the pinned SHA;
- live `Config.User` remained `1005:1005` and `Config.StopTimeout` remained 300;
- live port remained `127.0.0.1:55432`;
- active checkout HEAD remained
  `ff9c01d4e707867e54421db72d4fac779135f271`;
- active checkout status hash remained
  `6d345d7b44e025763c51961bae3abd865325f2c8c7bbb082f94132365531d962`;
- issue-owned synthetic container and work-root inventories were empty before
  and after.

No live tablespace DDL, business chunk movement, hypertable attach,
node-22/Slurm operation, production-path mutation or shared OpenSpec archive
occurred.

## Read-only live governance accounting

The exact-SHA collector read node-27 statvfs and PostgreSQL inventory without
writing a strict production governance receipt:

```text
/home:
  status=ok
  identity=253:1:11491905541504749415
  total=1780170539008
  used=1163974393856
  free=525692923904
  reserved=90503221248
  arithmetic=ok

/data/GHDC:
  status=ok
  identity=9:0:7539273700150526131
  total=15873497141248
  used=15506628608
  free=15057935572992
  reserved=800054939648
  arithmetic=ok

PostgreSQL:
  status=ok
  cold_relation_by_tablespace present=yes
  cold_relation_by_tablespace type=list
  rows=0
  bytes=0
  strict cold sample status=ok
  blockers=0
  database URL redacted=yes
```

Both filesystems had nonzero reserved bytes and independently satisfied
`total = used + free + reserved`. PostgreSQL returned a successful, present list
with zero rows, so zero cold-relation bytes is observed data rather than a
missing-status or malformed-inventory fallback.

The deployed strict descriptor-bound RAID/SMART/backup receipt is intentionally
not configured before #1895, as stated by
`infra/env/node27-resource-governance.example`. This probe therefore does **not**
claim the full production governance outcome is healthy; task 4.1/4.8 owns the
fresh root evidence, live thresholds and final production receipt.

## Phase 6.2 audit

A full-surface read-only audit covered every ten-action pending transition,
terminal publication, resolved-image/config/bind/device/runtime identity,
selector evidence boundary and dual-device accounting path. Its first pass
raised one candidate by treating the 2026-08-02 external snapshot's historical
tag-shaped `Config.Image` as current. Fresh node-27 inspect showed both current
image fields already carry the pinned digest. The reviewer re-adjudicated the
candidate as **REFUTED / stale-domain**: accepting tag-to-digest config drift
would violate #1894's exact reproduction rule. That invariant-audit verdict was
**clean; remaining findings none**.

The fresh Phase 7 Gap Sweep later found one P2 measured-zero boundary:
`optional_int(free_bytes) or -1` made observed zero indistinguishable from a
missing capacity observation. A same-invariant depth retro required a full
numeric truthiness inventory and central repair. At final semantic head
`56de13a1`:

- capacity decision carries `int | null` free bytes;
- zero remains measured data and receives the insufficient-headroom blocker;
- missing remains null and receives the distinct unavailable blocker;
- negative and bool/malformed observations remain fail-closed;
- exact run_install tests prove durable payload, no generic error, no authority,
  no Docker and no DDL.

The repeated Phase 6.2 audit and comprehensive Round 3 both finished clean with
zero candidates. The six-node oracle and read-only governance accounting in this
document were then rerun against the exact final semantic SHA above.

## Process deviations and routed follow-up

- Earlier delegated RED-proof work used stash despite the shared-stash ban. No
  temporary stash remains; the pre-existing #1707 stash retains SHA
  `68b37e4757ca5c21e4a3ff4b2fd4e75e7c452554` and protected untracked file `2`
  retains SHA-256
  `1853525a04fd662709c98f19705a544867785b456e2621b085befa245679ccc8`
  and size 148 bytes.
- The #1893 target-writability sibling defect remains outside #1894 and is
  tracked by https://github.com/DankerMu/SHUD-NWM/issues/1929. It blocks #1895.
