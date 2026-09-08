# node-27 receipt — #2158 output-geometry backfill scoped to the target river_network_version

- Date: 2026-09-08T23:18Z. Node: node-27 (`nwm@210.77.77.27`), production PG 15.18 on `127.0.0.1:55432`.
- Tree: throwaway worktree `/home/nwm/tmp/wt-2158` at `665842c8fc3564aa45a54f058878a30413e8fb05` (PR head); the active checkout
  `/home/nwm/NWM` stayed on `hotfix/node27-rollback-pre-2073` (`5a86841c`) and was not pulled or restarted.
- Interpreter: `/home/nwm/NWM/.venv/bin/python` (3.11.15) with `PYTHONPATH=/home/nwm/tmp/wt-2158`; module resolution confirmed:
  `workers.model_registry.basins_registry_import.__file__` → `/home/nwm/tmp/wt-2158/workers/model_registry/basins_registry_import.py`.
- Database: `NHMS_RUN_INTEGRATION=1 NHMS_INTEGRATION_DATABASE_URL=postgresql://nhms:***@127.0.0.1:55432/nhms` (owner role read from
  `infra/env/node27-timeseries-compression-replay.env` on the node, never echoed). The `integration_database_url` fixture creates and
  drops its own `nhms_it_<uuid>` database (`tests/conftest.py:164-178`); the live `nhms` database is not touched. First attempt with
  the `nhms_ingest_rw` role failed at `CREATE DATABASE` with `InsufficientPrivilege` and produced no test execution.
- Runner: `/home/nwm/tmp/node27-2158-runner.sh` (green → swap source to `origin/master` → red → restore → green), `TMPDIR=/home/nwm/tmp`.

## Command

```
NHMS_RUN_INTEGRATION=1 NHMS_INTEGRATION_DATABASE_URL=postgresql://nhms:***@127.0.0.1:55432/nhms \
TMPDIR=/home/nwm/tmp PYTHONPATH=/home/nwm/tmp/wt-2158 \
/home/nwm/NWM/.venv/bin/python -m pytest tests/test_backfill_geometry_network_scope_integration.py -q -p no:cacheprovider -rs
```

## 1. Green — PR head `665842c8` (2026-09-08T23:18:34Z)

```
1 passed in 2.27s
```

## 2. Red — source swapped to `origin/master` (`8a314ff2`) `workers/model_registry/basins_registry_import.py`

`git show origin/master:workers/model_registry/basins_registry_import.py > workers/model_registry/basins_registry_import.py`
(`1 file changed, 5 insertions(+), 11 deletions(-)` relative to the PR head), same test file, same database role:

```
>       assert b_after == b_before, (
E       AssertionError: backfill for it2158_rnv_a rewrote the same-id row in it2158_rnv_b:
E         (None, None, {'shud_riv_index': '1', 'shud_output_river': 'true'}, None)
E         -> ('MULTILINESTRING((110 30,110.6 30.6))', 1200.0,
E             {'Type': 3, 'shud_riv_index': '1', 'geometry_source': 'gis_rivseg_iRiv', 'shud_output_river': 'true',
E              'geometry_source_length_m': 1200.0, 'geometry_source_segment_count': 1}, 3.0)
E         (A's reach geometry is 'MULTILINESTRING((110 30,110.6 30.6))')
E         At index 0 diff: 'MULTILINESTRING((110 30,110.6 30.6))' != None
1 failed in 2.26s
```

Assertion (b) fails first: under the master UPDATE, network B's same-id output row received A's geometry, A's `length_m`, A's
provenance and the STORED `stream_type` 3.0. Assertions (a) passed before it (A itself was written correctly), so the red is the
cross-network write, not a seeding error.

## 3. Green — source restored (`git checkout -- workers/model_registry/basins_registry_import.py`, 0 dirty paths) (2026-09-08T23:18:41Z)

```
1 passed in 2.23s
```

## Cleanup

`git worktree remove --force /home/nwm/tmp/wt-2158 && git worktree prune` on node-27; the throwaway `nhms_it_<uuid>` database is dropped
by the fixture's `finally`. Raw logs: `/home/nwm/tmp/2158-{green1,red,green2}.log`, `/home/nwm/tmp/2158-summary.txt` (not committed).
