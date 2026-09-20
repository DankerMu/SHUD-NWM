## Context

Six guard-exempt files, ~13,250 lines total, must drop below the 1000-line
`.large-file-guard.json` threshold with their exemptions removed. The work is a
pure physical move: the hard part is not the move but the three ways a move can
silently destroy test signal.

Fixture level: `expanded`. Selected risk packs use the canonical vocabulary of
`issue-risk-contract.md` and are recorded with per-pack reasons in `tasks.md` §0;
the three that drive this design are **Public API / CLI / script entry** (the
display router and its OpenAPI document), **Concurrency / shared state /
ordering** (module-level `lru_cache`, `global`-mutated pool configuration and the
cold-generation gate), and **Legacy compatibility / examples** (~60 monkeypatch
sites, 15 source-text assertions, 44 frontend importers).

## Goals / Non-Goals

Goals:
- Every touched file and every new file <= 1000 lines, with headroom (target
  <= ~850 for files in the M27 growth path).
- The six exemption entries gone from `.large-file-guard.json`, and no new
  entry added for any new file.
- Zero behavior drift, provable mechanically, not by assertion.

Non-Goals:
- `apps/frontend/src/api/types.ts` (generated; stays exempt permanently).
- The other ~97 exemption entries.
- `services/tiles/mvt.py` slimming (itself exempt; separate debt).
- The `from apps.api.routes.pipeline import _ok` cross-route private import.
- Any refactor, bug fix, SQL change or contract change riding along.

## Decision 1: consumers stay on the facade, implementations move

`tests/` patches the `hydro_display` module object in ~60 places
(`monkeypatch.setattr(hydro_display, "<name>", fake)` across
`tests/test_hydro_display_mvt_scaling.py` and
`tests/test_display_mvt_cold_admission.py`, plus rebinds in
`tests/test_api_contract.py` and `tests/test_node27_connection_attribution.py`).

A re-export does **not** preserve monkeypatch semantics. Patching
`hydro_display.X` only affects a consumer that resolves `X` from
`hydro_display`'s own globals. So:

- If the consumer (a route handler) stays in `hydro_display.py` and imports the
  moved implementation with `from .hydro_display_<owner> import X`, then
  `hydro_display.X` is still the name the consumer resolves, and the existing
  patch keeps working **unchanged**.
- If consumer and patched name both move into the same new module, patching the
  facade becomes **inert**: the test stays green while testing nothing.

Therefore: **the route handlers and the `@router` decorations stay in
`apps/api/routes/hydro_display.py`.** Implementation bodies move out. Where a
moved helper is itself the consumer of another patched name, either that
consumer stays on the facade, or the patch is retargeted to the new module and
proved non-vacuous per Decision 3.

Candidate owner modules (verify against the code at implementation time; do not
copy these line ranges blindly):

- `hydro_display_postgis.py` — `_fetch_postgis_tile_bytes`, the four
  `_fetch_*_tile_bytes`, `_mvt_invalid_properties`, `_postgis_tile_params`,
  `_validate_supported_hydro_variable`.
- `hydro_display_identity.py` — `_river_network_source_version`,
  `_station_source_version`, `_station_active_flag`,
  `_require_hydro_mvt_source_identity`, `_require_run_source_identity`,
  `_run_source_version`, `_require_display_ready`, `_run_row`.
- `hydro_display_catalog.py` — `_default_layer_catalog`, `_empty_valid_times`.
- `hydro_display_instants.py` — `_reject_non_rfc3339_instant_text`,
  `_format_time`, `_require_seconds_precision_instant`,
  `_require_representable_instant`, `_national_source_cycle_tile_input`.

The router with tag `hydro-display` and the nine route handler function names
(`list_layers`, `list_discharge_cycles`, `list_layer_valid_times`,
`hydro_mvt_tile`, `hydro_national_source_cycle_mvt_tile`,
`hydro_national_mvt_tile`, `river_network_national_mvt_tile`,
`river_network_mvt_tile`, `met_station_mvt_tile`) stay in `hydro_display.py`;
the eight Pydantic classes (`Layer`, `ApiSuccessEnvelope`, `LayerListResponse`,
`LayerValidTimes`, `LayerValidTimesResponse`, `DischargeCycle`,
`DischargeCycles`, `DischargeCyclesResponse`) keep their class names wherever
they live, and the two tile description constants stay importable from
`hydro_display.py`: `tests/test_openapi_drift.py:59` is whole-dict equality and
FastAPI derives `operationId` from handler function names.

### Module-level shared state stays on the facade

`_engine` is an `lru_cache`'d factory that resolves `create_engine` from
`hydro_display`'s own globals; `_display_pool_configuration` mutates the
module-level `_DISPLAY_POOL_CONFIGURATION` through `global`; `_cold_generation_gate`
does the same with `_COLD_GATE` / `_COLD_GATE_LIMIT`. A facade re-export of a
mutable global creates an **independent binding**, so re-export cannot preserve
either the `global` mutation or the `create_engine` patch that
`tests/test_node27_connection_attribution.py` applies. Moving `_engine` would also
move the attributed engine root away from `apps/api/routes/hydro_display.py`,
which `REGISTERED_COMPONENTS` and `DISPLAY_UNIT_CONNECT_CLOSURE` in that test pin
to `nhms-display-api`.

Decision: the whole engine / pool-sizing / cold-gate / session block stays
physically in `apps/api/routes/hydro_display.py`. No `hydro_display_db.py` is
created. This costs about 105 lines of budget and buys zero retargets, zero
attribution-registry churn, and no shared-state aliasing. New owner modules are
still checked against the delegated closure registry, since they must be
classified there even when they open no connection.

### Dual-patch where a name legitimately has two homes

A patched name whose consumers end up in two modules (for example
`national_discharge_source_version`, consumed by `_default_layer_catalog` in the
catalog owner module and by three route handlers on the facade) is patched in
**both** modules at every affected test site. Over-patching is safe — it cannot
produce a silent pass — whereas picking one home leaves the other path running the
real implementation. Choosing a single arbitrary home is not allowed.

`apps/api/openapi_patching.py` follows the same shape but with an extra
constraint: patch **application order** and the `_finalize_openapi_schema`
timing must be byte-equivalent, and `openapi_patching` must keep
`_patch_mvt_tile_openapi` / `_patch_pipeline_openapi` as the *same function
objects* the owner modules define (plain `from ... import`, never a wrapper),
because `tests/test_openapi_drift.py` asserts `is` identity against
`apps/api/main.py`'s re-exports.

## Decision 2: barrel for the frontend, tests untouched

`apps/frontend/src/lib/m11/overviewDataContracts.ts` and
`apps/frontend/src/stores/overviewData.ts` keep their paths as barrels
(`export * from './...'`), export names unchanged, so all 26 + 18 importers and
every `__tests__` file stay byte-identical. No test mocks these two modules
directly (only `@/api/client`), so `vi.mock` / `vi.spyOn` redefinition hazards do
not apply — re-verify with a grep before splitting. Neither module has a
`export default`, so `export *` covers the whole surface.

One constraint on the store: `apps/frontend/src/stores/overviewData.ts` holds
module-level mutable state (`cache`, `overviewLoads`, the request nonces). ES module
re-exports are live bindings, so this is not the Python `global` hazard — but if that
state and its mutators land in two different submodules, `export * from` would newly
expose the state as named exports and break the "export names unchanged" guarantee.
Keep each piece of state and its mutators in one submodule, or use an explicit
re-export list instead of `export *`.

## Decision 3: three mechanical oracles, run per commit

1. **Pure-move oracle** — the moved code is identical, so with import lines and
   module docstrings stripped, the sorted non-import body of the new modules plus
   the facade equals the sorted non-import body of the original file. Any residue
   is either an intended facade line or an unintended edit, and must be named in
   the report. This is the proof that no SQL text changed.
2. **Non-vacuity oracle for every retargeted patch** — for each
   `monkeypatch.setattr` whose target module changed, flipping the target back to
   `hydro_display` must make the test **fail**. A retarget that passes both ways
   means the patch is inert and the test is not exercising the fake.
3. **Source-text assertion oracle** — 15 files under `tests/`/`scripts/`
   reference `apps/api/routes/hydro_display.py` as a string and assert SQL or
   shape against its text. Each must be repointed to the file that actually
   holds the moved SQL, and each repointed assertion must be shown to still
   match a non-empty slice (a repoint that yields `"" in text` passes vacuously).

## Risks / Trade-offs

- Batching three issues into one PR makes the diff wide. Mitigation: one commit
  per file family, each self-contained (split + its exemption removal + its
  registry updates), so review and revert stay per-family.
- `tests/test_hydro_display_mvt_scaling.py` at 4875 lines needs ~6 partitions;
  its SQL landmark `.index()/.rindex()` slices must migrate together with their
  `cte_start < cte_end < probe_start < probe_end` ordering preconditions, or they
  degrade to comparisons of empty strings.
- `scripts/select_ci_tests.py` fails closed on unregistered importers but fails
  *open* on unregistered partitions: a missed partition silently drops from the
  PR lane (and can downgrade the lane to a zero-assertion `--collect-only`
  smoke). `tests/test_select_ci_tests.py` closure guards plus an explicit
  selector run on this PR's own diff are the check.

## Migration Plan

Five serial commits on one branch, in dependency order:

A. `hydro_display.py` split (#2026) — must land first; the two test-file splits
   patch its private symbols.
B. `openapi_patching.py` split (#2074) — imports the tile description constants
   from the `hydro_display` facade.
C. `tests/test_hydro_display_mvt_scaling.py` split (#2074).
D. `tests/test_api_contract.py` split (#2074).
E. frontend barrel split (#2102).

## Open Questions

None blocking. Owner-module boundaries are recommendations; the implementer
re-derives them from the code at the time and reports any divergence.

## Appendix A: measured patch/consumer map for `hydro_display.py` (master ed1d94ced)

19 distinct names are patched on the `hydro_display` module object by `tests/`.
For each, the in-module consumers that must resolve the patched name:

| patched name | sites | defined here | in-module consumers |
|---|---|---|---|
| `display_catalog_cached` | 12 | no | `list_layers`, `list_layer_valid_times`, `list_discharge_cycles` (all routes) |
| `read_cached_tile_response` | 10 | yes | `_cached_or_generated_mvt_response` |
| `build_raw_tile_response` | 9 | yes | `_cached_or_generated_mvt_response` |
| `national_river_network_source_version` | 7 | no | `list_layers`, `river_network_national_mvt_tile` (both routes) |
| `display_ready_run` | 7 | no | `_run_row`, `_run_source_version`, `list_layers`, `list_layer_valid_times` |
| `_run_source_version` | 5 | yes | `_run_row`, `hydro_mvt_tile`, `list_layers` |
| `_river_network_source_version` | 5 | yes | `list_layers`, `river_network_mvt_tile` |
| `_mvt_live_postgis_enabled` | 5 | yes | `_default_layer_catalog`, `_require_live_postgis_mvt` |
| `_require_run_source_identity` | 3 | yes | `hydro_mvt_tile`, `list_layer_valid_times`, `list_layers` |
| `national_discharge_source_version` | 2 | no | `_default_layer_catalog`, `list_layers`, `hydro_national_mvt_tile`, `hydro_national_source_cycle_mvt_tile` |
| `create_engine` | 2 | no | `_engine` |
| `_DISPLAY_POOL_CONFIGURATION` | 2 | no | `_display_pool_configuration` |
| `_default_layer_catalog` | 2 | yes | `list_layers` |
| `tile_generation_lock` | 1 | no | `_cached_or_generated_mvt_response` |
| `national_discharge_valid_times` | 1 | no | `_default_layer_catalog`, `list_layer_valid_times` |
| `national_discharge_cycle_coverage` | 1 | no | `_fetch_hydro_national_mvt_tile_bytes` |
| `MVT_MAX_COORDINATES` | 1 | no | `_postgis_tile_params` |
| `_require_display_ready` | 1 | yes | `hydro_mvt_tile`, `list_layer_valid_times`, `list_layers` |
| `_fetch_postgis_tile_bytes` | 1 | yes | four `_fetch_*_tile_bytes` wrappers, `river_network_national_mvt_tile` |

### Rule derived from the table

- A patched name whose only consumers are route handlers needs **no test change**:
  the route stays on the facade, the facade imports the implementation by name,
  so `hydro_display.<name>` is still the binding the route resolves.
- A patched name whose consumers all move into one owner module is retargeted to
  that owner module (patch count above tells the cost).
- A patched name with **both** a staying route consumer and a moving helper
  consumer has two candidate targets. Resolve it by making the facade route call
  it through the owner module object, so exactly one target exists — or by
  keeping the helper on the facade. Never leave two targets.

### Budget check (why the boundaries below are the ones that fit)

Every route handler (428 lines) stays, and so does the engine / pool / cold-gate /
session block (~105 lines, per the shared-state decision above), plus the helpers
that share a patched name with a staying route: `_river_network_source_version`
(42), `_run_source_version` (25), `_run_row` (27), `_cached_or_generated_mvt_response`
(30) with `read_cached_tile_response` (8) and `build_raw_tile_response` (7). With
imports that floor is near 700 lines. The moves below take the file to roughly 830,
leaving about 170 lines of headroom:

| owner module | moves (approx. lines) | retargets needed |
|---|---|---|
| `hydro_display_postgis.py` | `_fetch_postgis_tile_bytes`, four `_fetch_*_tile_bytes`, `_mvt_invalid_properties`, `_postgis_tile_params`, `_validate_supported_hydro_variable` (~270) | `_fetch_postgis_tile_bytes` x1 and `MVT_MAX_COORDINATES` x1 to the owner module; the `river_network_national_mvt_tile` route must call `_fetch_postgis_tile_bytes` through the owner module object so exactly one patch target exists |
| `hydro_display_identity.py` | `_station_source_version`, `_station_active_flag`, `_require_hydro_mvt_source_identity`, `_require_run_source_identity` (~175) | none — every consumer is a route that stays on the facade. Verified: `_require_hydro_mvt_source_identity` does not call `_river_network_source_version`. `_require_display_ready` is **excluded from this move**: it calls `_run_row`, which stays (see Appendix B) |
| `hydro_display_catalog.py` | `_default_layer_catalog`, `_empty_valid_times`, `_mvt_live_postgis_enabled`, `_require_live_postgis_mvt` (~139) | `_mvt_live_postgis_enabled` x5 to the owner module; `national_discharge_source_version` x2 and `national_discharge_valid_times` x1 become dual-patch (facade + catalog); `_default_layer_catalog` x2 stay on the facade because `list_layers` stays |
| `hydro_display_instants.py` | `_RFC3339_INSTANT_RE`, `_reject_non_rfc3339_instant_text`, `_require_seconds_precision_instant`, `_require_representable_instant`, `_format_time`, `_national_source_cycle_tile_input`, `_validated_national_valid_time_selector` (~190) | none |
| `hydro_display_models.py` | the eight Pydantic classes (~55); class `__name__`s unchanged, so component schema names are unchanged | none |
| `hydro_display_constants.py` | layer definitions, response headers, `MVT_ROUTE_RESPONSES`, `TILE_X_DESCRIPTION`, `TILE_Y_DESCRIPTION` (~70); re-exported for `apps/api/openapi_patching.py` | none |

Total retarget cost: about 10 test sites, all in files this change already touches.

These are measured recommendations, not a mandate: re-derive from the code and
report any divergence with its reason. The binding constraints are the two rules
above (single patch target, or explicit dual-patch), the shared-state decision, the
1000-line ceiling with headroom, and the three oracles.

## Appendix B: reverse dependency scan (moved body -> names that stay on the facade)

The forward scan in Appendix A decides what must stay. This is the other
direction: for every body in the planned move set, the identifiers it loads that
would remain on the facade. Each such edge is either a constant that moves along,
or an import cycle (owner -> facade -> owner) that must be resolved by keeping the
consumer on the facade. Measured by AST walk over `origin/master` (ed1d94ced):

| moved body | stays-on-facade deps | resolution |
|---|---|---|
| `_fetch_postgis_tile_bytes` | `logger` | logger name is observable (see Appendix C); no pinned shared-state dependency, so the move is not blocked |
| `_require_display_ready` | `_run_row`, `DISPLAY_PRODUCT_READY_STATUSES` | `_run_row` stays on the facade (it consumes patched `display_ready_run` / `_run_source_version`), so **`_require_display_ready` stays on the facade too** — 16 lines, and it is patched x1 with route-only consumers, so keeping it costs nothing |
| `_default_layer_catalog` | `PUBLIC_LAYER_DEFINITIONS` | constant moves to `hydro_display_constants.py`, which the catalog module imports; no cycle |
| `_reject_non_rfc3339_instant_text` | `_RFC3339_INSTANT_RE` | regex moves with it into `hydro_display_instants.py` |
| `_national_source_cycle_tile_input` | `HYDRO_NATIONAL_SOURCE_ID`, `HYDRO_NATIONAL_SOURCE_VERSION` | constants move to `hydro_display_constants.py` |

No moved body touches any name pinned by the shared-state decision (`_engine`,
`create_engine`, `_DISPLAY_POOL_CONFIGURATION`, `_COLD_GATE*`,
`_release_session_checkout`, `_cold_generation_gate`, `_mvt_cold_generation_busy`,
`get_hydro_display_session`), so the engine block staying on the facade does not
block any planned move.

Rule: an import cycle is **never** resolved with a function-local
`from apps.api.routes import hydro_display`. That is a body edit disguised as a
move and it defeats oracle 1. Resolve it by keeping the consumer on the facade, or
by moving the shared constant into `hydro_display_constants.py`, which imports
nothing from the facade.

## Appendix C: the logger name is observable behavior

`apps/api/routes/hydro_display.py:82` is `logger = logging.getLogger(__name__)`,
and both `#2030` budget warnings (`MVT_TILE_BUDGET_TRUNCATED` at :965 and
`MVT_TILE_FEATURE_OVERFLOW_BLANKED` at :1000) are emitted inside
`_fetch_postgis_tile_bytes`, which moves. `tests/test_hydro_display_mvt_scaling.py:435`
pins `_TILE_ROUTE_LOGGER = "apps.api.routes.hydro_display"` and uses it in
`caplog.at_level(logging.WARNING, logger=_TILE_ROUTE_LOGGER)` with both positive
assertions and negative ones (`assert _truncation_records(caplog) == []`). A
negative assertion passes trivially if capture silently breaks, and the same
logger name is what routes these warnings to the stderr handler
`apps/api/main.py::_install_api_log_handler` installs for systemd.

Decision: the warnings keep the logger name `apps.api.routes.hydro_display`. Put
`logger = logging.getLogger("apps.api.routes.hydro_display")` in one small shared
module that the facade and the owner modules both import, so the name is identical
and no cycle is created. Do not let an owner module fall back to
`getLogger(__name__)`, and do not import the logger object from the facade.

Required evidence: show that a positive truncation assertion still observes the
record after the move, and that a negative assertion still fails when a warning is
forced — a `caplog` filter that captures nothing is a vacuous pass.

## Appendix D: patch-pattern scan beyond `setattr(hydro_display, ...)`

Searched for dotted-string patch targets, `mock.patch` / `patch.object`, aliased
module imports and logger-name coupling:

- No `monkeypatch.setattr("apps.api.routes.hydro_display.X", ...)` dotted-string
  sites, and no `mock.patch` / `patch.object` against this module.
- `tests/test_api_contract.py:16` imports it aliased as `hydro_display_routes`;
  that alias is the same module object, so the Appendix A dispositions apply to it
  unchanged.
- The only other dotted-string references are the logger name (Appendix C) and the
  CI selector registry strings (`tests/test_select_ci_tests.py:3959,12331`;
  `scripts/select_ci_tests.py`), both already task items.
