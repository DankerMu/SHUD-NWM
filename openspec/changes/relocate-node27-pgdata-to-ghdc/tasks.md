## 1. Contract

- [x] 1.1 Review this expanded fixture and invariant matrix; validate OpenSpec strictly.
- [x] 1.2 Freeze the CLI/state/ownership and disposable-oracle seams before implementation.

Fixture approval: `PgdataFixture` approved after the second bounded repair; B1-B4 closed. Implementation seams are the public CLI actions and durable `writes_released` boundary, existing container serializer contract, and existing `cold_governance_sample` output shape. Shared flags are `--action`, `--workspace`, `--source-container`, `--source-pgdata`, `--target-pgdata`, `--reserve-bytes`, `--mdadm-evidence`, repeated `--smart-evidence DEVICE=PATH`, `--enforce`, and isolated-oracle `--disposable-root`. Post-prepare actions use frozen workspace identity and refuse conflicting overrides. Core owns CLI/state/copy/serializer/tests; Placement owns governance attribution/tests and runbook/ADR; Main owns this fixture and integration.

## 2. Implementation

- [x] 2.1 Implement explicit plan/prepare/copy/activate/rollback/release with safe persistent state, fencing and exact topology checks.
- [x] 2.2 Reuse existing container/evidence/filesystem primitives and prove full offline copy before rebind, without changing cold-installer semantics.
- [x] 2.3 Correct governance PGDATA attribution and document rollout configuration without changing the currently deployed default.
- [x] 2.4 Add targeted behavioral regressions and the isolated node-27 physical-copy/rollback oracle.

## 3. Verification and delivery

- [x] 3.1 Run local lint, strict OpenSpec validation and actual read-only CLI smoke.
- [x] 3.2 Run node-27 focused tests and exact-image disposable copy/rebind/rollback/write-boundary proof; preserve production unchanged.
- [ ] 3.3 Complete independent high-risk review/verifier, final review, CI and PR evidence. Leave merge and production rollout human-gated.

## Risk triage

Issue: #2240; profile: NHMS. Fixture: expanded; repair intensity: high. Triggers: production configuration, whole-cluster file IO, persistent maintenance state, secrets and rollback/data-loss boundaries. Minimal mergeable slice is the complete tooling/rehearsal contract, not production relocation.

Selected packs: CLI (default read-only/enforce); config (exact snapshot/runtime and governance env); filesystem (complete copy, no-follow identity, scoped ownership); schema/fields (private state and receipt boundaries, no business schema change); auth/secrets (private config, read-only DB role); concurrency (fences, lifecycle lock, stopped source); resource limits (bounded metadata, streamed bytes and timeouts); legacy (same image/app and existing cold serializer); error/rollback (partial copy, rebind interruption, first-write marker); release/dependencies (exact native image tools, no upgrade); documentation (phase-specific operator procedure). Domain packs selected: PostgreSQL/Timescale physical integrity and published-read/ingest compatibility. Not selected: geospatial transformations, SHUD numerical behavior, Slurm and provider discovery, scientific file formats—none change.

## Seams under test

Public CLI actions and their observable source/container/unit effects; exact shared Docker serializer; root/path/copy boundary; durable state across a fresh process; governance storage sample attribution. Mock only process/filesystem boundaries in focused tests. Disposable Docker/PG exercises real data and the actual command path. Tests must reject plausible unsafe activation or stale rollback, not merely echo forwarding or freeze wording.

## Evidence Floor

- `openspec validate relocate-node27-pgdata-to-ghdc --strict --no-interactive`.
- Local `uv run ruff check` for changed Python and CLI default/help execution; backend pytest only on node-27.
- Node-27 tests: new relocation behavioral tests, affected container/governance tests and the disposable oracle. Export `TMPDIR=/home/nwm/tmp` before pytest and inspect `/`, `/home`, `/data/GHDC` capacity first. Code travels local commit -> GitHub -> isolated exact-SHA remote checkout; never edit product code remotely or replace the active checkout/runtime.
- Exact-image isolated proof: warm+compressed rows/metadata/roles preserved; default planning no mutation; dirty/partial/unsafe copy refusal; correct numeric ownership; verified bind-only activation; original restoration before release; stale rollback refusal after release; interrupted state cannot turn into success; no live identity admitted by a fixture. Existing cold-installer paths remain unchanged.
- Fixture repair B1-B3: reject activate/release from invalid predecessors; simulate a fresh process after stop/rename and lost ephemeral flock while durable unit fences/source Docker ID remain authoritative; read succeeds but a real ingest credential cannot connect/write even with explicit read-write intent before release; restore the exact original HBA only after the durable marker, and preserve stale-rollback refusal across a partial release.
- Governance: new target PGDATA counted once on its observed root; old `/home` sample remains correct; unknown attribution is not fabricated.
- Record commands, SHA and real output. Disposable proof is not live C1-C4/HDD performance or production relocation. Later rollout retains SQL1+20 P95<=300ms/buffers<=5000, API1+20 P95<=500ms, browser1+20 P95<2s, unchanged content and controlled-ingest/natural-tick gates. No silent SLO relaxation.

Verification recorded before review:
- `898ae126`: node-27 relocation/container/installer/recovery/governance/CI-selector suite, **856 passed** in 220.46s.
- `7b315abf`: node-27 relocation unit + exact-image CLI oracle + shared container/installer/recovery suites, **122 passed** in 121.21s. Includes real warm/compressed reads, writer rejection, three fresh-process rollback interruptions, partial release with committed business write, and retained partial copy.
- Local strict OpenSpec and changed-Python Ruff checks passed. Actual default CLI on Mac returned `mutation=false`, reported unavailable host tooling as a blocker, and created no workspace; the node-27 oracle separately exercised planning against a running disposable database.
- No production prepare/copy/rebind or root provisioning was performed. User-owned capacity hold and OLD runtime pins remain; live workload/backup gates remain outside this PR.

## Explicit non-goals

No live production migration, old-copy deletion, external tablespace/WAL relocation, SQL/schema/app/image upgrade, object-store move, reslice, cold-tier activation, node-22 operation or automatic post-write rollback. Full live workload acceptance and independently approved backup/disposal evidence belong to the later separately approved window; this is an intentional deployment boundary, not a claimed PASS.
