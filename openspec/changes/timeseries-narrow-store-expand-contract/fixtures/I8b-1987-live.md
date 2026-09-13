# Fixture I8b (#1987): current-runtime window and live evidence

## Authority and boundary

This consumes I8-1987's delivered 5.1 and governs 5.2; it does not erase historical receipts.
Effective tier high: production service configuration, schema rename, rollback and evidence identity.
Round 1 four canonical seats; later rounds at most three. Shared change stays active.
All task 5.2 evidence remains required; the schema/window milestone is not full issue completion.

User subsequently explicitly reordered delivery: merge the preparation changes first, then run
live and cross-day verification. This preparation merge does not claim 5.2 completion, close #1987
or #2280, or supply an execution receipt. Runtime code/runbook 5.1 are already merged. Post-merge
execution still obeys admission/rollback gates below. Notify the user immediately after this merge
with its exact commit so other sessions can continue; notify separately when the real window succeeds.

User authorized the window after #2273 review/CI, then confirmed #2162 is merged and other sessions
await #2280/#1987. User selected the bounded window, directed use of the new PGDATA/container and
explicitly assigned repair of the governance unit to this orchestrator. Do not repeat #2162.
Danker is window/DBA/runtime authorizer, Main executes. T0 starts only after admission; end T0+30 min.
If basic serving restoration is not ready ten minutes after API stop, cease forward progress and
enter the reviewed rollback process. Never force unknown sessions or skip a failed safety gate.

Current recovery source is `nhms-db`, host `/data/GHDC/nhms-primary/pgdata`, container PGDATA
`/home/postgres/pgdata/data`. Never launch a second PostgreSQL server on live PGDATA.
User subsequently clarified that PG has just been relocated, all original SHUD outputs remain,
and the database is rebuildable. Accept these user-reported facts; do not re-run a full physical
backup and whole-cluster restore merely to reconfirm them. The earlier TB-scale backup prerequisite
is superseded for this reversible expand, not for a future destructive contract.

Before schema mutation preserve private schema, run-route metadata and exact runtime/unit/env
snapshots; bind the current catalogue OIDs and source SHAs to the already-rehearsed D12 recovery.
Keep legacy facts and original SHUD artifacts. No contract, legacy DROP, sole-copy deletion or
DROP CASCADE. Newly narrow facts remain recoverable through retained rollback data and actual
old-parser reparse before any later removal. This is logical rollback/rebuild readiness, not a
claim of independent physical disaster recovery. Snapshot files may use the approved `/home`
location; a full physical backup or pg_verifybackup restore exercise is not a window blocker.

No node-22 mutation, physical PGDATA remigration, cold activation, foreign fence release or fixes for
issues #2282–#2285. Do not use `scripts/ops/start-display-api.sh`: #2282 documents cross-checkout killing.
Use the installed `nhms-display-api.service` directly and retain yd :8081 PID/health independently.

## Frozen source and observed baseline

OLD: `a8db554d6402bec642e9a05627eae64b2b79aec3`, clean active `/home/nwm/NWM`, branch
`hotfix/node27-rollback-pre-2073`, no upstream. NEW:
`1a32ebb7b536873e6403f3faeb6eb8d83ef24d32` (merged #2273 plus prior reviewed river implementation).
Do not follow a moving master. Receipt-only descendants must prove relevant source-byte identity.

Readonly inventory 2026-09-12T17:55:20Z: active Python 3.11.15; eight normal units have no drop-ins,
select active NWM, not the former frozen copy. Display :8080 and yd :8081 return health 200.
HOLD state authority remains `/home/nwm/.local/state/nhms-pgdata-pr-2240-capacity-hold/state.json`;
observed phase `lifted-and-fences-removed`, `resume-approved` present. Preserve its exact state.
Inactive historical transient reslice/prepare failures are not the live unit inventory and are untouched.

NEW's dependency delta from OLD adds only dev-extra mapbox-vector-tile; no production import was
found. Still verify NEW imports against the retained active interpreter before the window; no shared
venv rebuild. Private staging environments may be prepared beforehand.

## Governance unit repair: separately admitted before API outage

Existing unit receipt `resource-governance-20260912T165228Z.json` has exactly one critical:
`DATABASE_SIZE_ABOVE_CRITICAL`, bytes 1267493258031. OLD code treats it critical; reviewed I6/NEW
code treats database-size bands as info and protects the real projected working set instead.
This is deployment of already-reviewed semantics, not raising thresholds or masking a real warning.
The #2273 real readonly CLI on the same primary passed with destination binding and no criticals.

Repair only `nhms-node27-resource-governance.service` and its timer state. Stage the exact NEW source
in a dedicated owned checkout with a private venv outside ephemeral cleanup directories. Do not move
active NWM or change the other seven units before 000059. Retain that checkout while a unit references it.

Use the existing wrapper without modifying it. Its executable checkout key is
`NODE27_RESOURCE_GOVERNANCE_REPO` (not `_REPO_ROOT`); set its existing
`NODE27_RESOURCE_GOVERNANCE_ENV_FILE` to the same active env path. Live preflight found its
`NODE27_GOVERNANCE_PGDATA_ROOT` still points to `/home/nwm/nhms-pgdata`; correct only that assignment
to `/data/GHDC/nhms-primary/pgdata` after an on-node private backup and whole-file checksum comparison.
Preserve all other bytes and mode/owner; do not synchronize the real env across hosts. This required
configuration repair corrects the earlier unchanged-env assumption under the authorized governance
repair; it does not relax PGDATA binding or fresh-audit admission. `_REPO_ROOT` still describes the
audited production repository. Verify imported modules resolve to NEW. Keep original
journal/OnFailure/tee/PIPESTATUS behavior and private DB credentials.

The sole owned drop-in is `70-issue1987-governance-reviewed-source.conf`; refuse a pre-existing file,
symlink/unsafe parent or unexpected concurrent unit/config change. Save original unit/environment
hashes and exact timer enablement/activity before stopping only the governance timer. Create a
reviewed drop-in selecting the staged wrapper and working directory; do not install the repo base unit.
Run the actual service. Success requires a new execution, Result=success, ExecMainStatus=0, a new
complete audit with no criticals, actual PGDATA device binding, and unchanged display/yd PIDs/health
and other unit configuration. A lock-skip exit 0 or `reset-failed` is not success.

On staging failure preserve diagnostics, remove only the unchanged owned drop-in, reload and restore
original timer activity without altering enablement. Do not claim repaired. On success restore only
previously active timer activity and retain the staged runtime. After full cutover, remove this owned
pin only when active NWM is NEW and the ordinary governance service also passes with a fresh receipt.
No broad reset-failed, kill, mask, enable-now family operation or secret-bearing output.

## Window invariant and admission

At every serving/writing instant, code and canonical schema agree. Recovery preserves legacy facts,
new narrow run provenance and original SHUD artifacts. No sole-copy DROP or DROP CASCADE.
Before T0: reviewed recovery proof, actual catalogue/role/owner state, original SHUD artifact inventory,
baseline real query/API/probe evidence, runtime/import proof, current capacity proof and signed GO.
The prepared executor is reviewed before production use; SSH disconnect alone is never completion.
Use durable private execution state/logs and verify the remote result before any retry.

Inventory display plus autopipe, download, raw-retention, frontier-alert, MVT cache-retention,
timeseries compression, timeseries retention, resource-governance and installed compression replay.
Record each timer/service disposition, actual calendar, cgroup, locks, enable/mask state and source.
Stop affected timers first, services second; drain detached writers and DB sessions/locks. No silent
omission or substring state checks. Preserve unrelated/transient units and external connections.
Known governance failure is resolved by the real repair above, not ignored during drain.

Installed compression has one ExecStart and systemd wall 3940s, not the repo's 7842s/two-leg unit.
Do not install that unit or activate cold. Any bound-1 catch-up uses the reviewed §4.5 temporary
wall/env protocol, checking actual installed wall before launch and restoring exact original state.
A declaration-only budget PASS cannot certify systemd wall time. Retention uses installed calendars.

Fence ingress; require no serving from rename until validated restart. Restore the approved branch
without reset/force/stash-pop, ff-only to exact NEW. Enumerate ledger vs actual migration filenames;
only `000059_river_timeseries_narrow_expand.sql` may be pending. No accumulated migration batch.
Use explicit private DATABASE_URL and PGOPTIONS lock_timeout=5s, statement_timeout=120s through
retained `uv run --no-sync`; 000059 is one top-level DO statement, measured as such.

Before serving verify OID rename preservation, new key/enum-only shape, one-day dimension, indexes,
compression settings, nhms_ingest_rw ownership, routes, role audit and real representative narrow
parse/read plus legacy read. Use systemctl-only startup, prove actual API/autopipe execution source;
restore only authorized originally active timers last, with download's immediate-trigger behavior noted.
D12 uses actual OLD a8db and recorded pre-window unit/env state, never recreates historical 5a pins.

## Evidence and completion

Capture every task 5.2 item with actual source/request identity: one complete-cycle first chunk;
SHJ-NJ and small network × narrow-uncompressed/narrow-compressed/legacy; real SQL EXPLAIN and
at least five warm samples (SQL P95≤300ms, API P95≤500ms, Rows Removed/returned≤10, shared hit≤5000,
segment key index/segment pruning); actual miss probes/QHH fallback and coverage losses;
authoritative scheduler registry artifact counts; dual-table compression/retention tick; governance;
three-basin GFS/IFS screenshots, C1–C4 and /ops. Existing stale oracle refusal is not proof.
Any fact Seq Scan or order-of-magnitude regression blocks contract. Never force premature production
compression to manufacture a compressed-state receipt. Full-cycle/lag-dependent evidence may remain
open after the atomic window. Fourteen real daily receipts and zero legacy chunks belong to #1988.

Notify the user immediately when the atomic window, serving restoration and #2280 route verification
actually succeed, with exact SHA/ledger/runtime/health evidence and explicit remaining 5.2 items.
Do not make other sessions wait for natural compressed-state evidence, and do not close #1987 early.

Parent owns fixture, git, execution and receipts. Implementer owns temporary executor/config content;
reviewers/verifiers are readonly leaves and run no validation. No permanent runtime implementation
change is part of this docs+receipt issue. Archive exact executed drivers/digests, not placeholder proof.
