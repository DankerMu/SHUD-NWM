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

## Fresh identity: separate live projection, live refusal, synthetic execution

The production inspect literal from the exact checkout was:

```text
{"Mounts":{{json .Mounts}},"User":{{json .Config.User}}}
```

Its SHA-256 was
`711829df451c9abb189db02260bcdc3a551a4e9294b1d83e8ff60d10fa7507fb`.
The production owner retained the frozen 5-second and 65,536-byte ceilings.

Two different live observations were taken, and they are not the same thing. The
live `Config.User=1005:1005` below came from a separate bounded container
identity projection (the before/after `docker inspect` snapshot in "Production
and cleanup integrity"), not from a successful production inspector return: the
live topology has no cold bind, so the production inspector must refuse there.

```text
live_config_user=1005:1005
```

The production inspector therefore ran to completion only on the pinned-image
synthetic container `nhms-1929-target-c1f46e82` with its owner-matched
mode-0700 path, whose expected pair was seeded from that independently observed
live deployment value.

```text
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

### Live production inspector on the exact semantic SHA

Run read-only against the live `nhms-db` container from the exact detached
`c1f46e82` checkout. It refused, as it must while `nhms_cold` is absent: the
bind-missing check precedes any `Config.User` parse and any writability probe,
so no writable execution was invented on the live topology.

```text
live_production_inspector_outcome=refused
error_class=target_identity
stage=target_identity
reason=target inspector did not find exactly one cold tablespace bind
live_production_inspect_execve_count=1
live_docker_execve_count=0
live_writable_execve_count=0
live_container_identity_unchanged=true
active_checkout_unchanged=true
probe_sha=c1f46e823d359fd3af318131af3a5d2286951061
```

One `docker inspect` execve was observed; zero `docker exec` execves and zero
writable execves ran against the live container. Cleanup for this probe was
empty: it created no resources.

`strace -f -e trace=execve -s 4096` provided an execution oracle rather than
relying on the Python argv constructor alone. These counts are the synthetic
container's: the live topology contributed the single refusal inspect recorded
above and no exec of its own.

```text
numeric_execve_count=1
named_execve_count=1
production_inspect_execve_count=1
```

The exact numeric execution was
`/usr/bin/docker exec --user 1005:1005 nhms-1929-target-c1f46e82 test -w
/home/postgres/pgdata/tablespaces/nhms_cold`. It succeeded once. The otherwise
identical named-principal execution used `--user postgres`; the pinned image
resolved that name to `1000:1000`, and `test -w` returned 1. Therefore, on the
synthetic container, the measured equality is:

```text
fresh live Config.User (separately observed, seeds the expected pair)
  == configured expected == synthetic production-inspector observed
  == kernel-observed execve principal == 1005:1005
```

There was no root, image-default, UID-only, or named fallback. Proving the live
arm of that chain — live configured=observed=executed — requires the cold bind to
exist, so it is #1895 work: #1895 must fresh re-observe the current live
`Config.User` after installing the bind and then prove a successful live
production inspector return, not reuse this refusal or this synthetic success.

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

The live container identity projection — the separate bounded `docker inspect`
snapshot that supplied `live_config_user`, not a production inspector return —
covered container ID, resolved and configured image, numeric user, stop timeout,
start time, restart count, mounts and port bindings. It was byte-identical
before and after:

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

## Exact-semantic-head local verification record

The pre-commit working tree that became
`c1f46e823d359fd3af318131af3a5d2286951061` originally recorded the following
local results. Round 1 later replayed the load-bearing commands and separated the
reproducible measurements from two stale all-pass claims:

- focused target/runtime/CLI/schema integration surface: 510 passed, 2 skipped;
  this result reproduced exactly, and both skips were the expected locally
  disabled `NHMS_RUN_INTEGRATION` cases;
- selector contract: the original "434 passed" record was the collected total,
  not an all-pass result; pristine replay at this SHA produced 1 failed and 433
  passed, as detailed under "Selector route correction";
- full `uv run pytest -q`: the original "15,910 passed, 218 skipped" record is not
  retained as all-pass evidence because its selector-closure test was known to
  fail; no pristine full run at this SHA was performed during the correction;
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

The valid final local all-pass measurements are the post-fix figures under
"Selector route correction"; they cover the resulting Round 1 fix tree rather
than being retroactively attributed to `c1f46e82`.

The original evidence/task-only commit carrying this file followed the exact
tested semantic commit. Round 1 later changed only test oracles, CI test
selection, and this evidence attribution; the production inspector, runtime,
CLI, receipt writer and schema remain byte-identical to the node-27-tested
semantic SHA. A later semantic change to any of those production contracts
invalidates the node-27 oracle and requires rerunning it. Test/selector/evidence-
only corrections require fresh local verification and review, as recorded here,
but do not retroactively alter the measured remote container/database facts.

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
- Round 1 review of this PR confirmed two `test-evidence` defects in the shape
  recorded above. The preflight SQL-order tests asserted an empty execution log
  while driving the fake connection through its non-recording dispatch shortcut,
  which cannot fail; they now observe execution through the production
  `bind_execute` seam and state the real boundary (the read-only tablespace
  location SELECT precedes the inspector; a bad observation blocks attach
  queries, writability and movement). This file also credited `Config.User` to a
  live production inspector return on a topology where that inspector must
  refuse; the live value is now attributed to the separate bounded container
  projection and the live inspector is reported as the bind-missing refusal it
  actually was. Neither correction touches runtime, schema or CLI behavior, so
  the tested semantic SHA and every measured node-27 fact above are unchanged.
  A third `test-evidence` gap — a missing selector importer route — surfaced when
  these fixes were verified against the selector suite and is handled in
  "Selector route correction" below.

### Selector route correction

- Countering the previous section on its own terms: re-running the selector
  contract from a pristine `git archive` of `c1f46e82` yields 1 failed, 433
  passed, not the recorded 434 passed — 434 is the collected total, and
  `test_tests_support_module_rules_cover_their_non_gated_importer_closure` fails
  there and at `f03f43f7`. The failing pair is
  `tests/cold_residency_fakes.py -> tests/test_node27_cold_residency_schema_compat.py`:
  the new suite imports the shared fakes at file level and the
  `tests/cold_residency_fakes.py` routing rule in `scripts/select_ci_tests.py`
  never listed it. The parent commit `743e6f54` passes (420 collected), so the
  gap was introduced by this change, not by this correction. That failing test is
  collected by the full run, so the full-run figure above cannot have been
  all-pass at this SHA either — no full run was performed at pristine
  `c1f46e82` during this correction; the direct measurement is the Fix 1/Fix 2
  tree, which returned 1 failed, 15,912 passed, 218 skipped with this same
  failure. The focused 510 passed / 2 skipped figure did reproduce exactly.
- Fixed rather than left as a follow-up: the missing consumer was added to that
  one `PathTestRule`, the same edge was added to the explicit #1929
  producer-consumer contract table, and a load-bearing red leg
  (`test_cold_residency_fakes_rule_importer_edge_is_load_bearing`) deletes that
  consumer from the rule in memory and requires both the live selection and the
  generic closure guard to lose it. Removing the entry from the tracked rule was
  verified to red four independent guards, so the route is no longer decorative.
  No marker or gating logic was changed: an audit of every direct non-gated
  importer of `tests/cold_residency_fakes.py` found exactly one unrouted suite —
  this one — and the containment assertion in
  `test_cold_residency_fakes_rule_selects_runtime_proof_suite` keeps that
  invariant pinned. `tests/test_compressed_chunk_cold_target.py` remains an
  intentional over-route: it imports nothing from the fakes, and the closure
  guard tests containment rather than equality, so extras do not red it.
- Measured on the resulting Round 1 fix tree (Fix 1 + Fix 2 + Fix 3 together,
  not at `c1f46e82`):
  - `uv run pytest -q tests/test_select_ci_tests.py`: 435 passed, 0 failed;
  - focused 14-file target/runtime/CLI/schema surface: 513 passed, 2 skipped;
  - full `uv run pytest -q`: 15,914 passed, 218 skipped, 0 failed, with the same
    single pre-existing ecCodes version warning;
  - `uv run ruff check .`, strict OpenSpec validation, Markdown lint on this
    file, and `git diff --check`: PASS.
  These are the only local figures re-measured during Round 1; every node-27
  observation above is untouched, and the selector route change is a CI
  selection edit that alters no runtime, schema, CLI or receipt behavior.
