# Issue #1894 disposable install and rollback evidence

## Frozen oracle identity

- Issue: #1894
- Branch: `feat/issue-1894-cold-tablespace-install`
- Exact commit: `822e2dec67ed25bee295a51cad158eb181c9c5a7`
- Oracle: node-27 isolated checkout `/tmp/nhms-issue1894-oracle-822e2dec`
- Runtime: Python 3.11.15, PostgreSQL 15.2, TimescaleDB 2.10.2, pinned image
  `sha256:ad39c4fbc5c44557db1e16af10ec11e3ab12d0a472374f39aaba06ad9ca2640e`
- Isolation: synthetic names, non-55432 ports, and owned `/tmp` roots only. The
  production `nhms-db`, port 55432, active checkout, PGDATA, and `/data/GHDC`
  were observation-only integrity subjects, never mutation targets.

## Checked-in fixture matrix

- RAID states:
  - Healthy `[UU]`, degraded, rebuilding, recovering/reshaping,
    missing/substituted-member, and unknown fixtures are owned by
    `tests/test_node27_cold_tablespace_evidence.py`.
  - The named owners include
    `test_healthy_raid_and_two_descriptor_bound_smart_passes_are_admitted`,
    `test_nonhealthy_raid_states_are_blockers`, and the descriptor
    identity/freshness cases.
- SMART states:
  - Two-member PASS, member FAIL/unknown, and member-identity reuse refusal are
    owned by `test_smart_requires_each_parsed_member_to_explicitly_pass` and
    `test_single_smart_evidence_cannot_be_reused_for_both_member_identities`.
- Path and mount states:
  - Correct/wrong/missing mount; absent/nonempty/symlink/wrong owner, mode, or
    device; and resident PostgreSQL subtree fixtures are owned by
    `test_fresh_path_contract_rejects_every_unsafe_shape`,
    `test_resident_path_contract_accepts_postgres_version_subtree_and_rejects_unsafe_shapes`,
    and the installer resident/fresh-path tests.
- Catalog and bind states:
  - Absent/expected/drifted catalog, dangling current/stopped bind and
    `pg_tblspc`, no hypertable attach, and new-chunk `pg_default` placement are
    owned by `tests/test_node27_cold_tablespace_install.py`,
    `tests/test_node27_cold_tablespace_dry_run.py`, and the real happy-path
    oracle.
- Backup scope:
  - PGDATA-only versus PGDATA plus every external tablespace target is owned by
    `test_backup_inventory_requires_pgdata_and_every_external_tablespace_target`
    and
    `test_enforce_discovers_existing_external_tablespaces_before_accepting_backup_scope`.
- Raw Docker recreation:
  - Exact normalization/recreate/diff, supported non-default config, and Docker
    28 HostConfig defaults are owned by
    `tests/test_node27_cold_tablespace_container.py`; only the cold bind may
    differ.
- Rollback and recovery:
  - Stopped-container stale bind, empty-shadow refusal, referenced-path deletion
    refusal, and ownership-scoped rollback are owned by the installer/recovery
    contract suites and the real rollback oracle.
- Receipts and secret separation:
  - Dry-run/already-ready/NO-GO/progress/installed/pending-cleanup/rollback/error
    states are owned by the installer/recovery suites, schema, and examples.
- Governance:
  - Same-interval dual-device capacity, bounded trend, residual arithmetic, and
    healthy/drift receipts are owned by `tests/test_node27_cold_governance.py`
    and `tests/test_node27_resource_governance.py`.
- Root and runtime identity:
  - Root evidence setup, pinned-image fallback, numeric UID/GID, and helper
    cleanup are owned by the root-evidence/integration suites and the real
    node-27 oracle.
- Test selection:
  - Producer/consumer closure and opt-in marker isolation are owned by
    `tests/test_select_ci_tests.py` and
    `tests/test_node27_cold_tablespace_marker_contract.py`.

## Local verification at the oracle commit

```text
uv run pytest -q \
  tests/test_node27_cold_tablespace_install.py \
  tests/test_node27_cold_tablespace_recovery_contract.py \
  tests/test_node27_cold_tablespace_integration.py \
  tests/test_node27_cold_tablespace_authority.py \
  tests/test_node27_cold_tablespace_container.py \
  tests/test_node27_cold_tablespace_dry_run.py
```

- PASS: 131 passed, 5 skipped.
- `uv run pytest -q tests/test_select_ci_tests.py`
  - PASS: 417 passed.
- `uv run pytest -q tests/test_node27_cold_tablespace_marker_contract.py`
  - PASS: 4 passed.
- `uv run pytest -q`
  - PASS: 15563 passed, 217 skipped, one pre-existing ecCodes version warning.
- `uv run ruff check .`
  - PASS.
- `openspec validate compressed-chunk-cold-tablespace-tiering --strict --no-interactive`
  - PASS.

## Node-27 disposable Docker/PG/TimescaleDB oracle

The exact-SHA checkout ran only the tests carrying all three explicit opt-in
markers:

```text
NHMS_RUN_NODE27_DOCKER=1 uv run --directory \
  /tmp/nhms-issue1894-oracle-822e2dec pytest -vv -rs \
  -m "integration and timescaledb_210 and node27_docker" \
  /tmp/nhms-issue1894-oracle-822e2dec/tests/test_node27_cold_tablespace_integration.py
```

Result: 5 passed, 33 deselected in 148.38 seconds.

- `test_real_disposable_cluster_installs_through_run_install`: PASS. The same
  core state machine installed the cold bind/tablespace, published the observed
  final container digest, proved exact config diff, no hypertable attachment,
  new chunk/compressed siblings/indexes/TOAST in `pg_default`, then converged a
  second run to no-write `already_ready`.
- `test_real_post_recreate_failure_rolls_back_only_owned_state`: PASS.
- `test_real_interrupted_replacement_recovers_without_install_replay[prior_stopped]`: PASS.
- `test_real_interrupted_replacement_recovers_without_install_replay[prior_renamed]`: PASS.
- `test_real_interrupted_replacement_recovers_without_install_replay[replacement_created]`: PASS.

Wrapper terminal result: `pytest_rc=0`, `integrity_rc=0`.

## Production and cleanup integrity

The wrapper compared all integrity subjects before and after the test process:

- live container ID remained
  `93a0eb3586eaec59beb54d665be49d6f9defc1d8138f28af16a10f794c2f5f01`;
- live image remained
  `sha256:ad39c4fbc5c44557db1e16af10ec11e3ab12d0a472374f39aaba06ad9ca2640e`;
- live port remained `127.0.0.1:55432`;
- active checkout HEAD remained
  `ff9c01d4e707867e54421db72d4fac779135f271`;
- active checkout status hash remained
  `6d345d7b44e025763c51961bae3abd865325f2c8c7bbb082f94132365531d962`;
- issue-owned synthetic container and work-root inventories were identical
  before and after (both empty).

No live `nhms_cold` installation, business chunk movement, hypertable attach,
node-22/Slurm operation, production path mutation, or shared OpenSpec archive
occurred. Those rollout actions remain owned by #1895.

## Process deviation

During the final receipt regression fix, one delegated implementer used a
short-lived named `git stash` plus `stash pop` for RED proof despite the explicit
shared-stash prohibition. The orchestrator detected the violation immediately:
no temporary stash remained, the pre-existing #1707 stash still had exact SHA
`68b37e4757ca5c21e4a3ff4b2fd4e75e7c452554`, and the protected untracked file
`2` retained SHA-256
`1853525a04fd662709c98f19705a544867785b456e2621b085befa245679ccc8`
and size 148 bytes. No subsequent step used stash. This procedural deviation
changed no source/evidence content or production state and must remain listed in
the PR work summary.
