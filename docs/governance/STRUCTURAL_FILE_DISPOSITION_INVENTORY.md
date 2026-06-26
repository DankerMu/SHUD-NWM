# Structural File Disposition Inventory

Snapshot date: 2026-06-26

Scope: Governance-7 issue #668 current oversized source-file dispositions plus
Governance-8 owner-module updates as follow-up extractions land. This inventory
is evidence-only. It records the current disposition for the
mandatory-governance source files called out by the structural entropy budget
and the owner modules introduced to reduce those facades; it does not remove
compatibility surfaces, enable a hard gate, or write a baseline.

## Authority

This page is a companion inventory for
`openspec/changes/governance-7-structural-entropy-controls/`. When it
disagrees with the entropy audit schema, `docs/governance/entropy-budget.md`,
or `docs/governance/ROLE_BOUNDARY.md`, those sources win.

The structural-budget audit section reported
`schema_version: governance-7.structural-file-budget.v1`. All six files in
this inventory are present in `metadata.structural_file_budget.oversized_files`
with `budget_class: mandatory-governance`.

## Evidence Commands

Evidence was collected on 2026-06-23 with:

```bash
wc -l services/orchestrator/scheduler.py services/orchestrator/chain.py services/production_closure/two_node_e2e_evidence.py services/production_closure/readiness_validation.py apps/api/main.py apps/frontend/src/components/map/M11MapLibreSurface.tsx
uv run python scripts/governance/audit_repo_entropy.py --format json >/tmp/nwm-entropy-668.json
```

The line-count evidence was:

| Path | Current lines |
|---|---:|
| `services/orchestrator/scheduler.py` | 6328 |
| `services/orchestrator/chain.py` | 6956 |
| `services/production_closure/two_node_e2e_evidence.py` | 9098 |
| `services/production_closure/readiness_validation.py` | 3517 |
| `apps/api/main.py` | 2069 |
| `apps/frontend/src/components/map/M11MapLibreSurface.tsx` | 1499 |

API owner-module evidence was updated on 2026-06-26 with:

```bash
wc -l apps/api/main.py apps/api/openapi_patching.py apps/api/route_registry.py
uv run pytest -q tests/test_api.py tests/test_openapi_drift.py
uv run pytest -q tests/test_runtime_mode.py tests/test_role_boundary_static.py tests/test_api.py
uv run pytest -q tests/test_runtime_mode.py tests/test_role_boundary_static.py tests/test_api.py
uv run ruff check apps/api/main.py apps/api/openapi_patching.py apps/api/route_registry.py tests/test_api.py tests/test_openapi_drift.py tests/test_runtime_mode.py tests/test_role_boundary_static.py
openspec validate governance-8-module-deepening --strict --no-interactive
openspec validate --all --strict --no-interactive
cd apps/frontend && corepack pnpm check:api-types
git diff --check
```

Current API ownership line-count evidence:

| Path | Current lines |
|---|---:|
| `apps/api/main.py` | 401 |
| `apps/api/openapi_patching.py` | 1679 |
| `apps/api/route_registry.py` | 36 |

## Non-Targets

- No code movement or source-file extraction in #668.
- No compatibility removal, caller migration, route removal, API behavior
  change, frontend behavior change, production topology change, or evidence
  schema contraction in #668.
- No CI hard-gate enablement. The structural file budget remains report-only.
- No `.entropy-baseline/latest.json` creation or update.

## Priority Vocabulary

- `P1`: the file owns or fronts a near-term compatibility-facade or lane
  decomposition follow-up. New ownership surface should update this inventory
  or land in the owning follow-up issue.
- `P2`: the file stays frozen under scoped-context coverage before later
  extraction. New route, bootstrap, or display-map ownership surface should map
  to the scoped follow-up before being added here.

## Disposition Matrix

| Path | Current lines | Priority | Owner boundary | Disposition | Follow-up issue(s) | Retention/removal condition | Verification |
|---|---:|---|---|---|---|---|---|
| `services/orchestrator/scheduler.py` | 6328 | P1 | `compute_control` scheduler/orchestrator facade | Compatibility-facade freeze before extraction. Keep the current scheduler entrypoint stable while inventory and guard work identify real owner modules, callers, monkeypatch targets, parser/validator lanes, and compatibility symbols. | #669, #671 | Retain current facade and compatibility bindings until #669 records the governed symbol/caller inventory and #671 adds guard coverage or a recorded migration/removal decision. Do not add new import-family, public entrypoint, parser/validator, or compatibility surface without an inventory update. | Current #668 verification is `wc -l` plus the structural audit row. #669/#671 own focused scheduler facade tests and guard verification. |
| `services/orchestrator/chain.py` | 6956 | P1 | `compute_control` orchestration chain facade and aggregation surface | Compatibility-facade freeze before extraction. Keep the chain entrypoint stable while stage, manifest, reservation, retry, tile-publisher, worker, and persistence ownership is mapped. | #670, #671 | Retain current aggregation/facade behavior until #670 records owner modules and caller migration paths and #671 guards against new facade groups or ownership growth. Reduction is valid only behind stable entrypoints and focused behavior tests. | Current #668 verification is `wc -l` plus the structural audit row. #670/#671 own chain inventory, guard, and focused orchestration verification. |
| `services/production_closure/two_node_e2e_evidence.py` | 9098 | P1 | `compute_control` production-closure evidence aggregator with display-readonly evidence consumers | Lane-decomposition plan. Keep the aggregator entrypoint stable while Docker security, readonly DB, API/browser, logs, producer identity, source artifact, and manual ops receipt lanes gain explicit contracts. | #672, #674 | Retain the aggregator until #672 defines lane contracts and #674 extracts governed lane implementation behind equivalent result/status/blocker semantics. No path-safety, redaction, current-run, alias, or final aggregation behavior is removed by #668. | Current #668 verification is `wc -l` plus the structural audit row. #672/#674 own lane contract tests, equivalent-fixture checks, and production-closure verification. |
| `services/production_closure/readiness_validation.py` | 3517 | P1 | `compute_control` production-closure readiness aggregator | Lane-decomposition plan. Keep readiness aggregation stable while dependency summary, scheduler evidence, live proof, exclusions, and final readiness ownership is mapped. | #673 | Retain current aggregation until #673 records lane owner modules, result shape, blocker namespaces, and focused verification. Reduction is valid only when final readiness semantics stay equivalent for existing receipts. | Current #668 verification is `wc -l` plus the structural audit row. #673 owns readiness lane inventory and focused validation coverage. |
| `apps/api/main.py` | 401 | P2 | API bootstrap facade shared by `compute_control`, `display_readonly`, and `shared_contract`; OpenAPI patch owner is `apps/api/openapi_patching.py`; role-aware route registry owner is `apps/api/route_registry.py` | OpenAPI patch owner extracted in #756 and role-aware route registry extracted in #757. Keep `create_app()`, runtime config state, middleware, static/health/cache wiring, protected mutation guard, `runtime_config(request)`, `custom_openapi()`, `_custom_openapi_factory()`, and compatibility `_patch_*` imports stable while later static/startup/auth seams are extracted. | #686, #756, #757 | `apps/api/openapi_patching.py` owns runtime OpenAPI patch factory/order and runtime, pipeline, station-series, QHH latest-product, MVT, flood, met-stations, and layer metadata schema helpers. `apps/api/route_registry.py` owns business router ordering, runtime-router inclusion, and conditional Slurm router inclusion from `RuntimeConfig.slurm_routes_enabled`. Retain the `apps/api/main.py` facade and compatibility exports until a later recorded migration/removal decision with route inventory, role-boundary, OpenAPI drift, runtime-mode, and affected frontend type verification. No auth, request-body validation, route handler implementation, frontend UI, DB/schema, Slurm scheduling, scheduler, chain, or two-node behavior is removed by #757. | #756 verification: `uv run pytest -q tests/test_api.py tests/test_openapi_drift.py`; `cd apps/frontend && corepack pnpm check:api-types`. #757 verification: `uv run pytest -q tests/test_runtime_mode.py tests/test_role_boundary_static.py tests/test_api.py`; `uv run pytest -q tests/test_entropy_audit_script.py tests/test_runtime_mode.py tests/test_api.py`; `uv run ruff check apps/api/main.py apps/api/openapi_patching.py apps/api/route_registry.py tests/test_api.py tests/test_openapi_drift.py tests/test_runtime_mode.py tests/test_role_boundary_static.py`; `openspec validate governance-8-module-deepening --strict --no-interactive`; `openspec validate --all --strict --no-interactive`; `git diff --check`. |
| `apps/frontend/src/components/map/M11MapLibreSurface.tsx` | 1499 | P2 | `display_readonly` frontend map surface | Scoped-context coverage and freeze before later extraction. Keep the display map surface stable while map ownership, live-vs-mocked evidence rules, and later extraction boundaries are recorded. | #687 | Retain current surface until #687 records frontend map ownership and any later extraction preserves map behavior, product identity display, and frontend verification. No UI behavior is changed by #668. | Current #668 verification is `wc -l` plus the structural audit row. #687 owns focused frontend tests/build evidence for later map-surface work. |

## Per-File Notes

### `services/orchestrator/scheduler.py`

The audit reports 27 import-family tokens and ownership-surface signals for
many import families, public entrypoints, compatibility surface, and
parser/validator responsibility. That makes this a P1 facade-freeze target, not
an immediate line-count rewrite target.

### `services/orchestrator/chain.py`

The chain file is governed as an orchestration aggregation/facade surface.
Issue #670 should decide owner-module grouping by behavior lane, not by
arbitrary line ranges.

### `services/production_closure/two_node_e2e_evidence.py`

This is the largest current target and must be decomposed by evidence lane.
The stable entrypoint should continue to compose structured lane results until
the lane contracts in #672 and extraction work in #674 prove equivalent
blockers, redaction, path safety, and final status behavior.

### `services/production_closure/readiness_validation.py`

Readiness validation remains a P1 target because it mixes dependency proof,
scheduler evidence, live proof, exclusions, and final aggregation. Issue #673
owns the lane inventory before any extraction is claimed.

### `apps/api/main.py`

`apps/api/main.py` now retains the role-aware bootstrap facade while
`apps/api/openapi_patching.py` owns runtime OpenAPI patch generation and
`apps/api/route_registry.py` owns role-aware router inclusion. It remains P2
because later API bootstrap work must still extract static/health/cache/startup
and protected-mutation seams without changing runtime role boundaries or
OpenAPI behavior.

### `apps/frontend/src/components/map/M11MapLibreSurface.tsx`

The map surface is above the hard budget but remains a display-readonly
frontend surface. It is P2 because #687 must first record scoped map ownership
and verification expectations before extraction work claims improvement.

## Related Context

Issue #685 may improve scoped `services/production_closure/AGENTS.md` context,
but it is not a substitute for the file-specific lane dispositions in #672,
issue #673, and #674.
