# Governance-5 E1 Entropy Burn-Down Triage

This artifact is the #400 Governance-5 E1 triage snapshot. It turns the
current Governance-4 entropy report into burn-down dispositions. It is a
governance document only; it does not change audit schema, runtime behavior,
tests, CI workflows, frontend source, or API code.

## Snapshot

| Field | Value |
|---|---|
| Snapshot date | 2026-06-10 Asia/Shanghai |
| Report metadata timestamp | `2026-06-09T16:30:48+00:00` |
| Command | `PYTHONDONTWRITEBYTECODE=1 uv run --no-sync python scripts/governance/audit_repo_entropy.py --format json` |
| Mode | `report-only` |
| Schema | `governance-4a.entropy-report.v1` |
| Baseline state | `.entropy-baseline/latest.json` did not exist and was not written |
| Finding-bearing check families | 7 |
| Executed check families | 16 |
| Total findings | 360 |
| High-spread families | 4 |

## Current Counts

| Metric | Count |
|---|---:|
| Total findings | 360 |
| Allowlisted findings | 134 |
| Unallowlisted findings | 226 |
| P1 findings | 3 |
| P2 findings | 226 |
| P3 findings | 131 |
| High severity findings | 3 |
| Medium severity findings | 226 |
| Low severity findings | 131 |
| `display_readonly` findings | 245 |
| `shared_contract` findings | 115 |

## High-Spread Family Dispositions

| Family | Current count | Allowlist split | Current spread | Disposition | Owner |
|---|---:|---:|---|---|---|
| `apps-api-layer-inversion` | 3 | 0 allowlisted / 3 unallowlisted | 2 modules, top P1/high | `fix` | #399, `governance-5-e4-layer-inversion-hardgate-prep` |
| `stale-display-route-token` | 225 | 100 allowlisted / 125 unallowlisted | 12 modules, top P2/medium | `fix` | #397, `governance-5-e2-display-route-evidence-cleanup` |
| `placeholder-path-token` | 109 | 29 allowlisted / 80 unallowlisted | 15 modules, top P2/medium | `archived` | Governance-2/#363 for completed active-tree retirement; future #401/#402 semantics and guard work |
| `broad-e2e-api-mock` | 20 | 3 allowlisted / 17 unallowlisted | 1 module, top P2/medium | `fix` | #397, `governance-5-e2-display-route-evidence-cleanup`; Governance-2/#365 classification remains the prior mocked-vs-live source |

### `apps-api-layer-inversion`

Disposition: `fix`.

The current findings are active role-boundary defects, not historical evidence:
non-API modules import `apps.api.*`. The owner is #399 /
`governance-5-e4-layer-inversion-hardgate-prep`.

Current modules:

- `services/production_closure`
- `services/tiles`

Burn-down rule: remove the layer inversion in the owner change before this
family becomes future hard-gate eligible. #400 records the disposition only.

### `stale-display-route-token`

Disposition: `fix`.

The family mixes current route-authority cleanup with historical M26 evidence.
The actionable owner is #397 /
`governance-5-e2-display-route-evidence-cleanup`. Historical route references
should be classified or retained as evidence where appropriate; #400 does not
delete frontend routes or retire node-27 pages.

Current modules:

- `apps/frontend`
- `docs/governance`
- `docs/plans`
- `docs/runbooks`
- `openspec/governance-4-entropy-automation`
- `openspec/governance-5-e2-display-route-evidence-cleanup`
- `openspec/m20-production-multibasin-continuous-automation`
- `openspec/m21-qhh-hydro-met-ops-mvp`
- `openspec/m22-two-node-docker-readonly-display`
- `openspec/m25-multibasin-frontend-production`
- `openspec/m26-unified-map-display`
- `progress.md`

Burn-down rule: current docs and validation evidence should stop presenting
pre-M26 routes as primary live display entrypoints. Historical redirect,
compatibility, and worklog evidence should remain auditable.

### `placeholder-path-token`

Disposition: `archived`.

Governance-2/#363 already retired the active-looking placeholder paths. The
remaining high spread is primarily historical, archived, governance inventory,
or compatibility text. It is not a repeated deletion queue.

Current modules:

- `docs/archived`
- `docs/governance`
- `docs/modules`
- `openspec/governance-2-legacy-dead-code-retirement`
- `openspec/governance-4-entropy-automation`
- `openspec/governance-5-e1-entropy-baseline-burndown`
- `openspec/issue-122-publish-tiles`
- `openspec/issue-124-slurm-production-paths`
- `openspec/m0-engineering-init`
- `openspec/m1-gfs-forecast-loop`
- `openspec/m5-flood-frequency-warning`
- `openspec/m6-system-hardening-alignment`
- `openspec/m7-second-review-remediation`
- `openspec/m8-fourth-review-remediation`
- `services/slurm_gateway`

Burn-down rule: preserve governed historical and archived evidence, then let
future #401/#402 work add normalized allowlist semantics and tracked retired-path
guards. #400 does not delete QHH diagnostics or repeat Governance-2 placeholder
retirement.

### `broad-e2e-api-mock`

Disposition: `fix`.

The findings are deterministic mocked frontend regression evidence that can be
mistaken for live display proof. The owner is #397 /
`governance-5-e2-display-route-evidence-cleanup`, which consumes the
Governance-2/#365 mocked-vs-live classification instead of reopening it.

Current modules:

- `apps/frontend`

Burn-down rule: keep deterministic mocked coverage clearly separated from live
node-27 display receipts. #400 does not edit Playwright specs or implement
finding-level gate eligibility.

## Owner Notes

- #397 owns the display route authority and mocked-vs-live evidence cleanup
  mapped from `stale-display-route-token` and `broad-e2e-api-mock`.
- #398, `governance-5-e3-api-contract-retirement`, has no current high-spread
  family assigned by this snapshot. It remains the API contract retirement owner
  if later entropy or contract inventory work surfaces actionable legacy API
  route findings.
- #399 owns the active `apps-api-layer-inversion` defects.
- #401/#402/#403 remain future automation/report work and are not implemented by
  #400.

## Explicit Non-Goals

- No QHH diagnostic deletion, relocation, wrapper insertion, or helper-chain
  rewrite.
- No repeated Governance-2 placeholder deletion.
- No CI hard-gate enablement and no workflow change.
- No node-27 frontend route/page retirement, source migration, test migration,
  or visual evidence movement.
- No audit script schema change, allowlist normalization, finding-level
  hard-gate eligibility implementation, tracked retired-path guard, or refreshed
  report example schema.
- No API endpoint deprecation, removal, OpenAPI contraction, or generated type
  regeneration.

## Governance-7 Active Budget Delta

This 2026-06-24 UTC update records the report-only audit after Governance-7
active drift cleanup issues #675, #676, and #677. It is evidence/worklog only;
it does not change detector logic, runtime behavior, source code, or baseline
state.

| Field | Value |
|---|---|
| Command | `uv run python scripts/governance/audit_repo_entropy.py --format json >/tmp/entropy-678-current.json` |
| Mode | `report-only` |
| Report metadata timestamp | `2026-06-24T05:21:58+00:00` |
| Total findings | 448 |
| Global `budget_counted_count` | 223 |
| Global `gate_eligible_count` | 0 |
| Baseline written | `false` |
| Non-archive budget-counted route/path findings | 0 |

Governance-7 design used 36 non-archive budget-counted route/path findings as
the active cleanup target. The current report has 0 remaining non-archive
budget-counted findings for `stale-display-route-token`,
`placeholder-path-token`, and `placeholder-path-exists`, after excluding archive
material under `docs/archived/**` and `openspec/changes/archive/**`.

No active owner mapping is required because the non-archive route/path remainder
is zero. The remaining global budget-counted findings are archive route/path
semantics under `openspec/changes/archive/**`; those are intentionally not
claimed as fixed here. Governance-7 archive-status issues #679, #680, and #681
remain the owners.

Verification:

- `uv run python scripts/governance/audit_repo_entropy.py --format json
  >/tmp/entropy-678-current.json` passed.
- `git diff -- .entropy-baseline/latest.json --exit-code` passed.

## Governance-8 Module Deepening Delta

This 2026-06-26 UTC update records the report-only audit after Governance-8
owner-family groups 1 through 6 completed. It is evidence/worklog only; it does
not change detector logic, runtime behavior, source code, CI hard gates, or
baseline state.

| Field | Value |
|---|---|
| Command | `uv run python scripts/governance/audit_repo_entropy.py --format json >/tmp/nwm-entropy-768.json` |
| Mode | `report-only` |
| Report metadata timestamp | `2026-06-26T17:14:31+00:00` |
| Schema | `governance-4a.entropy-report.v1` |
| Total findings | 822 |
| Global `budget_counted_count` | 338 |
| Global `gate_eligible_count` | 0 |
| Baseline exists | `true` |
| Baseline written | `false` |

### Structural File Budget Delta

The six Governance-7 mandatory source facades now total 21,449 lines, down from
the Governance-7 structural inventory baseline of 29,467 lines, for a net delta
of -8,018 lines. This delta is not a hard-gate claim; it is the report-only
evidence snapshot for #768.

| Path | Governance-7 baseline | Current lines | Delta | Current report class |
|---|---:|---:|---:|---|
| `services/orchestrator/scheduler.py` | 6328 | 6815 | +487 | `mandatory-governance` |
| `services/orchestrator/chain.py` | 6956 | 8222 | +1266 | `mandatory-governance` |
| `services/production_closure/two_node_e2e_evidence.py` | 9098 | 4526 | -4572 | `mandatory-governance` |
| `services/production_closure/readiness_validation.py` | 3517 | 1193 | -2324 | `mandatory-governance` |
| `apps/api/main.py` | 2069 | 339 | -1730 | below current report threshold |
| `apps/frontend/src/components/map/M11MapLibreSurface.tsx` | 1499 | 354 | -1145 | below current report threshold |

Current structural budget metadata:

- `mandatory_governance_count`: 99
- `governed_exemption_count`: 4
- oversized report class split: 99 `mandatory-governance`, 0 other oversized
  classes in this report

### Compatibility-Facade Guard Delta

The report-only compatibility-facade guard is clean:

| Facade | Inventory | Status | Signal count |
|---|---|---|---:|
| scheduler | `docs/governance/SCHEDULER_COMPATIBILITY_INVENTORY.md` | `ok` | 0 |
| chain | `docs/governance/CHAIN_COMPATIBILITY_INVENTORY.md` | `ok` | 0 |

### Scoped-Context Delta

The scoped agent context report is clean:

| Metric | Count |
|---|---:|
| Governed scopes | 4 |
| Missing instruction files | 0 |
| Missing glossary links | 0 |
| Stale context signals | 0 |
| Total scoped-context signals | 0 |

| Scope | Status |
|---|---|
| `services/orchestrator` | `pass` |
| `services/production_closure` | `pass` |
| `apps/api` | `pass` |
| `apps/frontend` | `pass` |

### Report-Only Finding Summary

| Metric | Count |
|---|---:|
| Allowlisted findings | 484 |
| Unallowlisted findings | 338 |
| Budget-counted findings | 338 |
| Not budget-counted findings | 484 |
| P2 findings | 344 |
| P3 findings | 478 |
| `display_readonly` findings | 708 |
| `shared_contract` findings | 114 |

Current finding families:

| Check ID | Count |
|---|---:|
| `stale-display-route-token` | 702 |
| `placeholder-path-token` | 112 |
| `broad-e2e-api-mock` | 6 |
| `openapi-frontend-types-delegated` | 1 |
| `openapi-frontend-types-signal` | 1 |

Verification:

- `uv run python scripts/governance/audit_repo_entropy.py --format json
  >/tmp/nwm-entropy-768.json` passed.
- `uv run pytest -q tests/test_entropy_audit_script.py` passed.
- `git diff -- .entropy-baseline/latest.json --exit-code` passed.

## Governance-8 Final Local Verification Gate

This 2026-06-26 UTC update records the #769 local final verification gate after
Governance-8 inventory synchronization and report-only entropy delta recording.
It is evidence/worklog only; it does not replace future node-27 live receipt
requirements for runtime behavior changes.

| Surface / group | Issue / PR authority | Command | Result |
|---|---|---|---|
| Global lint | #769 | `uv run ruff check .` | PASS, all checks passed |
| Global OpenSpec | #769 | `openspec validate --all --strict --no-interactive` | PASS, 184 items |
| Global diff hygiene | #769 | `git diff --check` | PASS |
| Frontend wrapper command | #769 | `cd apps/frontend && pnpm test` | BLOCKED before Vitest by local `ERR_PNPM_IGNORED_BUILDS` for `esbuild@0.25.12`; no tests executed |
| Frontend wrapper command | #769 | `cd apps/frontend && pnpm build` | BLOCKED before Vite by local `ERR_PNPM_IGNORED_BUILDS` for `esbuild@0.25.12`; no build executed |
| Frontend map/API type successor gate | #766 / PR #825 | `cd apps/frontend && corepack pnpm test` | PASS, 34 files / 616 tests |
| Frontend map/API type successor gate | #766 / PR #825 | `cd apps/frontend && corepack pnpm build` | PASS, Vite build completed |
| Scheduler group | #720 / PR #779 | `uv run pytest -q tests/test_production_scheduler.py tests/test_scheduler_backfill.py tests/test_gateway_reconcile.py` | PASS, 641 tests |
| Chain group | #731 / PR #790 | `uv run pytest -q tests/test_orchestration_chain.py tests/test_retry_cancel_consistency.py tests/test_real_database_integration.py` | PASS, 239 tests, 7 skipped |
| Two-node and readiness groups | #744 / PR #803; #755 / PR #814 | `uv run pytest -q tests/test_two_node_e2e_evidence.py tests/test_production_readiness_validation.py` | PASS, 1181 tests, 2 skipped |
| API bootstrap group | #760 / PR #819 | `uv run pytest -q tests/test_static_serving.py tests/test_runtime_mode.py tests/test_api.py tests/test_role_boundary_static.py tests/test_monitoring_api.py tests/test_openapi_drift.py` | PASS, 240 tests |

The local `pnpm` wrapper failure is a workstation dependency-approval boundary,
not a frontend test/build regression. The equivalent project command via
`corepack pnpm` executed Vitest and Vite successfully. No baseline, generated
API type, runtime source, DB/schema, Slurm, production topology, station-MVT, or
live display evidence was changed by this verification slice.
