# R1.3 governance owner-transfer implementation fixture

Issue #1895 / epic #1891; upstream tasks R1.3, governance portion R1.5 and
matching R3.7 env-template row. Suggested fixture expanded accepted; effective
high. Prerequisite PGDATA transfer merged PR #2311. No dependency on later
C4/SQL or compression cuts.

Minimal mergeable slice: move ordinary collection to
`node27_resource_governance_collection.py`, detach the resource-governance
CLI/config/output from cold receipt/evidence/history machinery, migrate actual
callers/tests/selector/docs and remove the cold option template block. Retain
cold-only modules and their direct tests for the later R3 owner-family deletion;
never leave compatibility exports of moved generic functions.

## Must-preserve and intentional removal

- Retain filesystem/device/available-space collection, `/data/GHDC` observation,
  existing ordinary receipt/sample keys (including `filesystems.cold` as the
  pre-existing `/data/GHDC` observation label, not cold activation). No
  unnecessary field rename or version bump.
- Preserve #2273 actual configured PGDATA path/device/free-byte binding;
  unknown/conflict refuses comparison rather than falling back to `/home`. Keep
  #1985 discovery/lag/watermark and working-set projection behavior.
- Preserve #2277/#1769 maintenance output collection: per-relation effective
  thresholds and bounded rows, freshness/unavailable findings, ordinary
  recommendations and critical CLI exit/diagnostic contract. Do not reinterpret
  cold-only inventory failure as an ordinary healthy observation.
- Preserve generic SQL collection, filesystem/syscall/subprocess bounds and
  secret redaction. Do not change SQL transaction or connection behavior
  incidentally.
- Remove `--cold-governance-*` CLI options, corresponding
  `AuditConfig`/environment fields, cold receipt/runtime/history branch,
  `cold_tablespace_governance` output, and cold-only catalog inventory from
  ordinary collection (`cold_tablespace`, `cold_relation_by_tablespace`,
  external-target inventory used solely by cold backup evidence). Unknown
  removed CLI options refuse through normal argparse, no aliases/no-ops.
- Move only ordinary collectors and their required helpers; cold sample
  accounting stays cold-owned until R3. Any actual remaining cold caller of
  moved helpers imports the real owner directly (qualified module references
  prevent accidental re-export).
- Mixed resource tests: keep/repoint ordinary
  capacity/device/workingset/maintenance tests; move still-live cold-only
  sample/receipt tests to the existing cold test owner rather than carrying cold
  runtime imports in ordinary suites. Remove tests exclusively demanding the
  intentionally removed CLI cold integration; do not remove protective ordinary
  behavior.
- Retire cold env-template comments with CLI removal. Repoint current runbook
  source references without reviving withdrawn G0–G8. No effective production
  unit/drop-in/env change: node-27 currently has an issue1987 governance source
  pin owned elsewhere.
- New collection-owner PathTestRule explicitly selects
  `tests/test_node27_resource_governance.py`, `tests/test_node27_working_set.py`,
  `tests/test_node27_maintenance_output_integration.py` and
  `tests/test_node27_cold_governance.py` (retained cold sample consumes the moved
  observation helper). The stable resource-governance CLI rule also selects
  maintenance integration. Update expected maps in `tests/test_select_ci_tests.py`
  atomically; producer edits do not gain test-import closure automatically.
  Do not reproduce the old rule's missing maintenance integration consumer.
  Remaining cold schema/example/CLI selection cleanup belongs to R3.5.
- Repoint the current storage runbook §9.2 pg_stat_activity reader attribution
  from `node27_cold_governance_collection.py:246` (original line 7561) to
  `node27_resource_governance_collection.collect_postgres`, without a brittle
  replacement line number or change to the measured privilege conclusion.

## Risk packs

Selected: CLI/script entry (main/parser/critical exit); config/setup (inert env
defaults/removal); filesystem/path (statvfs/du, output); schema/fields (optional
cold outputs removed, ordinary wire preserved); auth/secrets (readonly
credentials/redacted diagnostics); concurrency/ordering (collect/close/emit);
resource limits (bounded SQL/du/time); legacy compatibility/examples (ordinary
keys and actual callers); error/partial-output (unavailable samples and critical
recommendations); packaging/dependencies (source/test/selector closure);
docs/migration notes (owner and removed inputs). Domain selected:
PostGIS/TimescaleDB (real metadata and maintenance query), published NHMS
evidence identity (actual PGDATA bytes/device binding). Not selected:
geospatial/CRS, hydro-met time semantics beyond unchanged discovery/lag, SHUD
runtime/numerics, Slurm, external providers, manifest/QC (no changes).

## Invariant Matrix

Governing invariant: ordinary governance remains truthful, bounded, readonly and
secret-safe after losing cold topology/receipt support; the configured PGDATA
device and preserved maintenance signals remain the source of truth.

| Surface             | Concrete seam / scenario                                                            | Evidence                                                                                                           |
| ------------------- | ----------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------ |
| Producers           | `collect_filesystem`, `collect_postgres`, `collect_working_set` with healthy inputs | Same ordinary sample keys/value meanings; only retired inventory omitted; no cold dependency                       |
| Validators/binding  | PGDATA missing device/free bytes or mismatched usage identity                       | Unknown/conflict blockers and null available comparison; never fallback to home                                    |
| Storage/query       | Existing SQL catalog + maintenance collectors, empty/healthy/unavailable data       | Actual PostgreSQL/Timescale integration, effective thresholds/row bounds and readonly/close behavior retained      |
| Entry               | CLI normal audit, critical recommendation, removed cold argument                    | Normal main receipt/exit/diagnostic preserved; removed arg refused without producing cold receipt                  |
| Consumers           | Working-set tests, maintenance integration, PGDATA host admission and CI selector   | All direct source/test imports and producer/test helper closure migrate, selector points to existing assertions    |
| Failure             | DB/sampling failure and stale maintenance outputs                                   | Stable nonhealthy recommendations, bounded failure, no raw DSN/stderr leakage or fabricated zero health            |
| Evidence/deployment | Audit summary paths and current source guidance                                     | Existing summary behavior, schema version and `/data/GHDC` observation preserved; no live unit mutation/PASS claim |

Boundary checklist: new generic owner; readonly catalog/filesystem reads; audit
summary writes; actual PGDATA binding; critical exit and maintenance staleness;
changed producer/test consumers; old cold receipt branch removal; untouched
active pinned service. Source references:
`scripts/node27_resource_governance.py` imports/config/parser/build_receipt;
`node27_cold_governance_collection.py` generic functions vs
`_cold_relation_bytes`/`cold_governance_sample`;
`tests/test_node27_working_set.py`;
`tests/test_node27_maintenance_output_integration.py`; resource mixed tests;
selector rules. Preserve modules within current size guards, no new exemptions.

## Verification / Evidence Floor

- Local full Ruff, strict active OpenSpec + affected canonical specs and scoped
  Markdown.
- Node-27 isolated checkout/venv, owned TMPDIR/basetemp below `/home/nwm/tmp`;
  capacity check `/ /home /data/GHDC` before tests; never active checkout/venv
  or production database for tests.
- Targeted:
  `uv run pytest -q tests/test_node27_resource_governance.py tests/test_node27_working_set.py tests/test_node27_cold_governance.py tests/test_node27_pgdata_migrate.py tests/test_select_ci_tests.py`
  plus moved/helper suites identified by actual caller closure.
- Real engine: existing `tests/test_node27_maintenance_output_integration.py` on
  an independently created disposable PostgreSQL/Timescale cluster with
  appropriate integration opt-in; do not substitute a DB inside production. Keep
  its pre-existing harness/markers and exact environment requirements.
- Real readonly CLI smoke at main, no cold env: write receipt to owned temporary
  path; assert actual device/free-byte binding and maintenance status. A live
  production catalog audit is readonly observation only and must not change the
  issue1987 effective source pin, services or database. Missing cold flag is
  tested with argparse refusal, not a help-only smoke.
- `uv run pytest -q` default backend on node27. Existing behavior test migration
  requires no fabricated red proof; newly needed changed-behavior tests must
  discriminate the old cold output/CLI path at pre-change source (batched red)
  or concrete mutation for a retained boundary.
- Match R4.1: Main applies PGDATA placement/governance MODIFIED delta with this
  slice; retained SQL performance ADDED requirement remains later R1.4. Update
  tasks only for this delivered slice; global R1.5/R3/R5 remain incomplete.
- Before merge: high-risk four-seat round1, independent verifier, final
  exact-head review, CI and Chinese summary. User preauthorized merge, not
  effective deployment changes.

Non-goals: cold package-family deletion, compression decoupling, new generic
storage/RPO/RTO project, C4/SQL workload transfer, maintenance policy changes,
production DDL/REVOKE/drop-ins, node22, archive or epic closure.
