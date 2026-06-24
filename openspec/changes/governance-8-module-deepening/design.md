## Context

Governance-7 established the current authority for six oversized source files:

- `services/orchestrator/scheduler.py`
- `services/orchestrator/chain.py`
- `services/production_closure/two_node_e2e_evidence.py`
- `services/production_closure/readiness_validation.py`
- `apps/api/main.py`
- `apps/frontend/src/components/map/M11MapLibreSurface.tsx`

The authoritative design inputs are the Governance-7 structural disposition and
lane/compatibility inventories, scoped `AGENTS.md` files, current source code,
and focused tests. The root `IMPLEMENTATION_PLAN.md` is historical context only.

This change is a module-depth phase, not a behavior-expansion phase. The
expected outcome is smaller stable interfaces with more behavior hidden behind
owner modules. Every slice must preserve current runtime behavior while making
the next behavior change easier to localize and test.

## Goals / Non-Goals

**Goals:**

- Preserve stable public entrypoints and compatibility surfaces.
- Preserve legacy compatibility surfaces until inventory-backed caller
  migration and focused tests prove removal is safe.
- Move implementation families into owner modules with focused tests on the
  owner-module interface and compatibility tests on the facade interface.
- Keep final aggregators intact until all lane result interfaces are stable.
- Split implementation issues by ownership family rather than by arbitrary line
  ranges.

Stable entrypoint matrix:

| Capability | Stable surfaces that must not regress |
| --- | --- |
| Scheduler facade | `ProductionScheduler`, `ProductionSchedulerConfig`, scheduler CLI/default factory paths, `services.orchestrator.scheduler` legacy imports/re-exports/monkeypatch paths, and scheduler compatibility lookup behavior |
| Chain facade | `ForecastOrchestrator`, `AnalysisOrchestrator`, `OrchestratorConfig`, chain result/context types, `SlurmGatewayClient`, `HttpSlurmGatewayClient`, and `services.orchestrator.chain` legacy imports/re-exports/monkeypatch paths |
| Two-node E2E evidence | `validate_two_node_e2e_evidence(config)`, module CLI behavior, lane summary schema, final summary schema, blocker/finding namespaces, and output safety semantics |
| Readiness validation | `validate_readiness(config)`, `validate_readiness_item(item)`, validate-readiness CLI behavior, readiness item schema, final summary schema, `_final_ready` semantics, and release-blocker shape |
| API bootstrap | `create_app(env=None)`, `runtime_config(request)`, role-aware route registration, error/middleware behavior, static/health routes, OpenAPI schema output, and protected mutation fail-closed behavior |
| Frontend map surface | `M11MapLibreSurface`, exported map helper contracts, source/layer IDs, interaction payloads, popup slot/curve-window behavior, selected-feature data attributes, and station-MVT scope separation |

**Non-Goals:**

- No public API route removal, database migration, Slurm scheduler behavior
  change, production topology change, or display-readonly capability expansion.
- No station-MVT backend closure; that remains a separate authority.
- No entropy hard-gate enablement and no `.entropy-baseline/latest.json` write.
- No deletion of compatibility exports or monkeypatch paths unless a task
  explicitly includes caller migration, inventory update, and parity evidence.

## Decisions

### Decision 1: Stable facade first, owner extraction behind it

All six groups keep their current caller-facing interface while owner modules
take over implementation behavior. Facades remain narrow shells that compose
owner modules and preserve old import/monkeypatch paths.

Alternative considered: rewrite callers to new owner modules first. Rejected
because scheduler and chain tests intentionally patch old private names, and a
caller-first migration would make behavior regressions harder to distinguish
from import-path churn.

### Decision 2: Extract lane contracts before final aggregators

For two-node E2E and readiness validation, shared item/lane contracts,
producer/current-run binding, redaction, path safety, and status semantics are
extracted before final aggregation. Final aggregation moves last.

Alternative considered: move final aggregation early to reduce file size.
Rejected because it would recreate the same coupling in a new file before
lane-result interfaces are stable.

### Decision 3: Use compatibility inventories as extraction checklists

Scheduler and chain implementation issues must cite the relevant inventory
group and update that inventory when the facade surface changes. Adding a new
re-export, wrapper, compatibility alias, or local implementation without
inventory coverage is a regression.

Alternative considered: rely only on tests. Rejected because tests catch
observable behavior but do not reliably catch ownership-surface growth.

### Decision 4: API bootstrap splits app assembly from OpenAPI patching

`apps/api/main.py:create_app()` remains the public assembly entrypoint. OpenAPI
schema patching, router registration, static/health mounting, and startup cache
warmup move to focused modules only when runtime-role behavior remains
equivalent.

Alternative considered: route-by-route cleanup first. Rejected because the
oversized surface in `main.py` is mostly bootstrap/OpenAPI composition, not
route implementation.

### Decision 5: Frontend map splits pure builders before interactive hooks

The M11 map first moves pure GeoJSON/source/layer builders and MapLibre
primitive renderers into focused modules. Interaction dispatch and popup/
selection coordination move only after layer IDs, click priority, hover state,
camera updates, and unavailable/error states have focused tests.

Alternative considered: create one large `useM11MapSurface` hook. Rejected
because it would hide the same mixed responsibilities behind a new shallow
interface.

## Migration Plan

1. Add or strengthen compatibility/contract tests for the owner family being
   extracted.
2. Move implementation behind the owner module without changing the stable
   entrypoint.
3. Keep facade wrappers/re-exports in place and update inventory when the
   compatibility surface changes.
4. Update the structural, compatibility, or lane inventory in the same PR as
   the owner-family extraction. The update must record the new owner module,
   retained facade surface, removal condition, and focused verification command.
5. Run the focused verification listed for that owner family.
6. Only after all families in a group are complete, run the broader group
   verification and record structural entropy/audit deltas.

Rollback is ordinary Git revert per issue because each issue owns one module
family and must keep stable entrypoints compatible. No data migration rollback
is expected.

## Risks / Trade-offs

- Compatibility paths may drift while implementation moves -> preserve and
  extend compatibility guard tests before extraction in scheduler and chain.
- A lane extraction may silently change blocker/finding semantics -> require
  same summary shape, status ordering, blocker/finding namespaces, redaction,
  and final aggregation parity tests.
- A readiness extraction may treat deterministic evidence as live proof ->
  keep `validate_readiness_item` truth table and `_final_ready` semantics as
  explicit shared contracts before moving validators.
- API bootstrap cleanup may expose Slurm/control routes in display_readonly ->
  keep runtime role tests and role-boundary static tests in every API slice.
- Frontend map cleanup may change click priority or selected-feature state ->
  add tests for station-over-river priority, layer IDs, filters, and popup
  placement/identity behavior before moving interactive code.
- PR count will be larger -> acceptable because each PR is smaller, easier to
  review, and safer to roll back.

## Issue-Ready Task Rules

Every implementation issue generated from this change must include:

- `Module/Scope`: one owner family or lane, not an arbitrary line range.
- `Dependencies`: prerequisite issues or `None`.
- `Out of Scope`: adjacent behavior that must not be changed in that PR.
- `Focused Verification`: exact command(s), including any `-k` selector.
- `Inventory/Evidence Update`: the document or evidence mapping updated in the
  same PR, or an explicit `No facade/lane surface change` statement backed by
  tests.

Group-level verification tasks remain integration gates. They do not replace
the focused verification and inventory/evidence update required by each owner
family issue.

## Open Questions

- None that block issue creation. Each implementation issue must remain
  implementation-ready and must not push product decisions into the
  implementation phase.
