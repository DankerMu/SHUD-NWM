# Node-27 Isolated Probe Routing Receipt

Captured: 2026-08-30

Scope: issue #1914, `node27-isolated-probe-opt-in` OpenSpec task 2.5.

## Frozen Isolated Execution

- Source code SHA: `841c4d6fb9df6e34da6d166afcbd29160c9a8191`.
- Detached clone: `/tmp/nhms-1914-frozen-841c4d6f`.
- Active checkout: `/home/nwm/NWM`, untouched (`active_checkout_unchanged=true`).
- Python: existing virtual environment, Python 3.11.15; no sync or environment rebuild.

The exact and only selected test node was:

```text
tests/test_probe_compressed_chunk_cold_tablespace.py::test_isolated_cluster_probe_is_opt_in
```

It was explicitly selected with `-m timescaledb_210`. The dummy URL only unlocked
collection and was never connected; `production_database_connected=false`.

```text
pytest=1 passed in 16.43s
pytest_exit_code=0
```

## Disposable Probe Evidence

```text
probe.status=passed
accepted_sequence=shell_first_decompress_recompress_atomic
image_pin_ok=true
pg_matches_pin=true
ts_matches_pin=true
report_sha256=85ea06378dac0d9f3ccb798d2bf24659484db76ed640c9b26796ae465a58f02e
report_bytes=287000
```

Cleanup was complete and ownership-bound:

```text
created_container=true
container_removed=true
container_absent=true
work_root_absent=true
identity_bound=true
port_55494_clear=true
disposable_container_baseline_restored=true
disposable_workdir_baseline_restored=true
temporary_clone_removed=true
```

## Live Database and Checkout Boundary

Before/after live `nhms-db` identity and status evidence matched exactly:

```text
id=93a0eb3586eaec59beb54d665be49d6f9defc1d8138f28af16a10f794c2f5f01
status=running
restart_count=0
image_id=sha256:ad39c4fbc5c44557db1e16af10ec11e3ab12d0a472374f39aaba06ad9ca2640e
config_image=sha256:ad39c4fbc5c44557db1e16af10ec11e3ab12d0a472374f39aaba06ad9ca2640e
live_container_unchanged=true
```

No live database query or mutation occurred. The active checkout remained unchanged;
all probe state was confined to the disposable container and work root.

## Durable Evidence

The remote durable evidence root is `/home/nwm/nhms-receipts/issue-1914`. Its key
files include:

- `receipt-summary.json`
- `probe-report.json`
- `report-semantic-summary.json`
- `pytest.log`
- before/after live identity and status evidence files
- before/after disposable container and workdir baseline evidence files

This sanitized receipt contains no credentials, authentication material, or real DSN.

## Verdict

PASS for OpenSpec task 2.5:

| Task 2.5 clause | Evidence |
| --- | --- |
| Frozen-head explicit marker execution | Detached clone at `841c4d6fb9df6e34da6d166afcbd29160c9a8191` ran the single stated test node with `-m timescaledb_210`; pytest passed with exit 0. |
| Disposable probe PASS and cleanup identity | Probe status passed; image, PostgreSQL, and TimescaleDB pin gates were true; every ownership-bound cleanup field was true. |
| Active `nhms-db` and checkout unchanged | Before/after `nhms-db` identity/status matched, restart count remained 0, and `/home/nwm/NWM` was untouched. |
| No production business database mutation | The dummy URL was never connected, `production_database_connected=false`, and no live database query or mutation occurred. |

No generic GitHub CI-green claim is made here. PR CI remains required after this
receipt is committed.
