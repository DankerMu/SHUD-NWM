# Issue #1929 numeric runtime identity evidence

## Frozen identity and isolation

- Issue: #1929.
- Exact tested semantic commit:
  `c1f46e823d359fd3af318131af3a5d2286951061`.
- Branch: `feat/issue-1929-cold-target-runtime-identity`.
- Oracle host: node-27, 2026-09-01.
- Exact detached checkout:
  `/tmp/nhms-1929-target-c1f46e82/repo`; imported production module:
  `/tmp/nhms-1929-target-c1f46e82/repo/packages/common/compressed_chunk_cold_target.py`.
- Runtime: Python 3.11.15, PostgreSQL 15.2, TimescaleDB 2.10.2.
- Pinned image:
  `sha256:ad39c4fbc5c44557db1e16af10ec11e3ab12d0a472374f39aaba06ad9ca2640e`.
- One-shot wrapper SHA-256:
  `c81de09651dea0c058cdc03e6ea28f677a9f7b8a8a35f49204167947c2ba9fce`.
- Synthetic resources were limited to container
  `nhms-1929-target-c1f46e82` and root
  `/tmp/nhms-1929-target-c1f46e82`. The wrapper refused pre-existing same-prefix
  resources and removed only a container carrying both
  `nhms.oracle.issue=1929` and the exact-SHA label.

The live `nhms-db` container and live PostgreSQL cluster were read-only
observation subjects. The wrapper contained no `/home/nwm/nhms-pgdata`,
`/data/GHDC`, port 55432, tablespace DDL, `ALTER TABLE`, or `SET TABLESPACE`
command. Every live SQL summary ran inside `BEGIN READ ONLY`; the synthetic
container used `--network none`, `--read-only`, `--cap-drop ALL`,
`no-new-privileges`, and a bind under the synthetic `/tmp` root. No node-22 or
Slurm operation ran. The absent production cold bind/tablespace was not created:
that remains #1895 work.

## Fresh expected, observed and executed identity

The production inspect literal from the exact checkout was:

```text
{"Mounts":{{json .Mounts}},"User":{{json .Config.User}}}
```

Its SHA-256 was
`711829df451c9abb189db02260bcdc3a551a4e9294b1d83e8ff60d10fa7507fb`.
The production owner retained the frozen 5-second and 65,536-byte ceilings.
One production inspector invocation against the live container freshly observed
`Config.User=1005:1005`. The same production inspector was then exercised on the
pinned-image synthetic container and its owner-matched mode-0700 path.

```text
live_config_user=1005:1005
expected_pair=1005:1005
observed_pair=1005:1005
executed_numeric_pair=1005:1005
numeric_writable=true
host_mode=0700
host_owner=1005:1005
image_config_user=postgres
named_postgres_pair=1000:1000
named_writable_rc=1
```

`strace -f -e trace=execve -s 4096` provided an execution oracle rather than
relying on the Python argv constructor alone:

```text
numeric_execve_count=1
named_execve_count=1
production_inspect_execve_count=1
```

The exact numeric execution was
`/usr/bin/docker exec --user 1005:1005 nhms-1929-target-c1f46e82 test -w
/home/postgres/pgdata/tablespaces/nhms_cold`. It succeeded once. The otherwise
identical named-principal execution used `--user postgres`; the pinned image
resolved that name to `1000:1000`, and `test -w` returned 1. Therefore the
measured equality is:

```text
fresh live Config.User == configured expected == production observed
                       == kernel-observed execve principal == 1005:1005
```

There was no root, image-default, UID-only, or named fallback.

## Zero live DDL and zero chunk movement

The read-only signature included:

- complete `pg_tablespace` identity/location/ACL/options rows;
- every tablespace attached to either allowed business hypertable;
- every allowed business chunk's schema/name/window/compression state;
- origin and compressed heap, index, TOAST heap and TOAST index OIDs, names and
  effective tablespaces.

The before and after files were byte-identical:

```text
live_signature_sha256=0c10cb1701a76f019e53364f290559706ed961fc6209fce26de2f26bf51512b0
live_signature_unchanged=true
business_chunk_count=10
compressed_chunk_count=6
physical_residency_member_count=94
business_hypertable_attach_count=0
tablespace_count=2
nhms_cold_count=0
live_sql_transactions=read_only
live_ddl_issued=0
live_chunk_movement_issued=0
```

This combines a command-boundary proof (the live SQL surface had no mutating
statement and PostgreSQL reported `transaction_read_only=on`) with a state proof
(the full physical residency signature was unchanged). It does not infer zero
movement from aggregate counts alone.

## Production and cleanup integrity

The live container identity projection covered container ID, resolved and
configured image, numeric user, stop timeout, start time, restart count, mounts
and port bindings. It was byte-identical before and after:

```text
live_container_id=93a0eb3586eaec59beb54d665be49d6f9defc1d8138f28af16a10f794c2f5f01
live_container_config_user=1005:1005
live_container_image=sha256:ad39c4fbc5c44557db1e16af10ec11e3ab12d0a472374f39aaba06ad9ca2640e
live_container_started_at=2026-08-28T15:15:28.758848584Z
live_container_restart_count=0
live_identity_sha256=ca08abae138781656d6e02bbfc066c6695b1dcb6f92dd8dcd561194db89b1ad4
live_container_identity_unchanged=true
```

The active checkout was also byte-for-byte unchanged at its status boundary:

```text
active_checkout_head=ff9c01d4e707867e54421db72d4fac779135f271
active_checkout_status_sha256=d657a3f4eff013f0eb5afa6f970c60686016c0b924bab0bf34c9ea0f80035fc6
active_checkout_unchanged=true
```

Independent post-checks found zero `nhms-1929-target-*` containers and zero
same-prefix `/tmp` roots. The live read-only post-check still reported 10
business chunks, 6 compressed chunks, and no `nhms_cold` catalog row.

```text
synthetic_container_cleanup=empty
synthetic_root_cleanup=empty
oracle_result=PASS
```

## Exact-semantic-head local verification

The final implementation working tree that became
`c1f46e823d359fd3af318131af3a5d2286951061` passed:

- focused target/runtime/CLI/schema integration surface: 510 passed, 2 skipped;
  both skips were the expected locally disabled `NHMS_RUN_INTEGRATION` cases;
- selector contract: 434 passed;
- full `uv run pytest -q`: 15,910 passed, 218 skipped, with one existing ecCodes
  version warning;
- `uv run ruff check .`: PASS;
- `openspec validate compressed-chunk-cold-tablespace-tiering --strict
  --no-interactive`: PASS;
- shipping schema against all five 1.1 examples plus historical 1.0 reader and
  recovery compatibility tests: PASS;
- real Docker CLI production-template parse and anti-vacuity defective-template
  probe: PASS;
- repository entropy audit: zero gate-eligible findings; production owner line
  counts were 912, 227, 630 and 806;
- `git diff --check`: PASS.

The evidence/task-only commit carrying this file follows the exact tested
semantic commit. Any later semantic change to the inspector, runtime identity,
CLI, receipt/schema, selector, or relevant tests invalidates this oracle and
requires rerunning it.

## Process deviations and routed limit

- The implementation subagent accidentally used `git checkout` to restore two
  test files it had damaged and once used bare `python3` for a read-only JSON
  dump. No stash or residual state remains, but both actions violated the
  workflow/tooling discipline.
- Initial implementations exceeded the runtime-owner responsibility budget, used
  Docker's unsupported Sprig `dict` template, and let an over-width decimal reach
  Python 3.11 `int()`. Those paths were rejected by entropy, real-CLI and boundary
  probes, then replaced before the frozen semantic commit.
- The sibling generic positive-integer parser/tombstone defect predates #1929 and
  remains deliberately out of scope. It is tracked by
  https://github.com/DankerMu/SHUD-NWM/issues/1938.
