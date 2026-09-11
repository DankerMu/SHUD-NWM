## Why

Node-27's primary database shares the smaller `/home` filesystem with other services. Rebuilding existing billion-row chunks addresses a subset of that pressure through expensive logical rewrites; relocating the complete PostgreSQL cluster preserves its already-built indexes and gives the primary access to `/data/GHDC` capacity instead. The user selected a separate migration PR, not an immediate production cutover.

## What Changes

- Add a dry-run-first, checkpointed offline physical-cluster migration command with explicit maintenance preparation, copy, activation, pre-business-write rollback, and write-release boundaries.
- Preserve the exact PostgreSQL/TimescaleDB image, complete cluster, application runtime, roles, ports, and unrelated container settings. Replace only the PGDATA host bind; never promote the partial reslice verification database.
- Reuse existing safe filesystem, exact Docker snapshot, and descriptor-bound root RAID/SMART evidence primitives. Keep the existing cold-tablespace installer semantics unchanged.
- Provide a disposable node-27 oracle and a deployment/acceptance runbook, including governance placement settings, HDD performance gates, rollback limits, and old-copy disposal prerequisites.
- Keep production rollout and old-directory deletion human-gated. This PR neither migrates production nor claims that a disposable rehearsal establishes production HDD performance.

## Capabilities

### New Capabilities

- `node27-pgdata-relocation`: Whole-cluster offline copy, exact container rebind, recoverable pre-write activation, and explicit write-release authority.

### Modified Capabilities

None. Existing compressed-chunk residency, business query, and lifecycle policy contracts remain unchanged; any necessary placement-observation integration must preserve their existing defaults.

## Impact

Primary surfaces are `scripts/node27_pgdata_migrate.py`, narrowly scoped helpers under `packages/common/`, container serialization reuse, behavioral tests and a disposable Docker oracle, and storage operation documentation. No SQL migration, data model change, frontend deployment, object-store move, node-22 change, package upgrade, or new recurring service is included. Existing #1891/#1895 cold-tier work explicitly excludes whole PGDATA and is not reused as authorization for this operation.
