## 0. Risk pack pass (canonical vocabulary)

- [x] 0.1 Record the pack verdicts below and keep each mapped item satisfied.
  - **Public API / CLI / script entry — SELECTED.** The display router, its nine
    route handlers and the runtime OpenAPI document are the public surface.
    Mapped to: `tests/test_openapi_drift.py` (whole-dict equality + facade
    identity) and `git diff origin/master -- openapi/nhms.v1.yaml` empty.
  - **Concurrency / shared state / ordering — SELECTED.** `_engine`'s `lru_cache`,
    the `global`-mutated `_DISPLAY_POOL_CONFIGURATION`, and the
    `_COLD_GATE` / `_COLD_GATE_LIMIT` gate cannot survive a re-export as an alias;
    `openapi_patching`'s patch application order is also order-sensitive. Mapped
    to: the shared-state decision in design.md (that block stays physically on the
    facade), plus `tests/test_node27_connection_attribution{,_delegated}.py`,
    `tests/test_display_mvt_cold_admission.py` and
    `tests/test_mvt_tile_generation_lock.py` green.
  - **Legacy compatibility / examples — SELECTED.** ~60 monkeypatch sites, 15
    source-text assertions, 44 frontend importers, and the `main.py` re-export
    identity assertions all depend on unchanged surfaces. Mapped to: design.md
    Decisions 1-3 and oracles 1-3, and the affected-suite list in 1.1.
  - **Resource limits / large input / discovery — SELECTED.** The whole point is a
    line-count limit, and the CI selector's discovery of test partitions decides
    what the PR lane executes. Mapped to: tasks 4.1 and 4.4, plus
    `tests/test_select_ci_tests.py`.
  - **Documentation / migration notes — SELECTED.** Three issues are closed by one
    PR, so the batching deviation and the per-family commit mapping must be
    written down. Mapped to: proposal.md "Deviation from upstream issue shape" and
    the PR `偏离记录`.
  - **Schema / columns / units / field names — NOT selected.** No SQL text, column,
    unit or field name changes; oracle 1 (pure move) is the proof.
  - **Auth / permissions / secrets — NOT selected.** No auth code path is moved or
    changed; `tests/test_slurm_gateway_openapi_security.py` still runs because it
    imports `openapi_patching`, as a regression check only.
  - **File IO / path safety / overwrite — NOT selected.** No filesystem behavior is
    touched; the only path-valued changes are registry entries.
  - **Config / project setup — NOT selected.** No env var, CLI flag or settings
    change; `.large-file-guard.json` edits are exemption removals only.
  - **Error handling / rollback / partial outputs — NOT selected.** Error paths move
    verbatim with their bodies; no new failure mode is introduced.
  - **Release / packaging / dependency compatibility — NOT selected.** No
    dependency, version or packaging change.

## 1. Backend source splits

- [x] 1.1 Split `apps/api/routes/hydro_display.py` (1686 lines) below the guard
      threshold (#2026).
  - Module/Scope: move implementation bodies to the owner modules in design.md
    Appendix A (`hydro_display_postgis.py`, `hydro_display_identity.py`,
    `hydro_display_catalog.py`, `hydro_display_instants.py`,
    `hydro_display_models.py`, `hydro_display_constants.py`). All nine route
    handlers and the `APIRouter` with tag `hydro-display` stay on the facade.
    Target facade size ~830 lines, ceiling 1000.
  - Shared-State Decision (explicit, from design.md): `_engine`, `create_engine`,
    `_APPLICATION_NAME`, `_bounded_env_int`, the pool-sizing helpers,
    `_DISPLAY_POOL_CONFIGURATION`, `_display_pool_configuration`, `_COLD_GATE*`,
    `_cold_generation_gate`, `_effective_cold_limit`, `_release_session_checkout`,
    `_mvt_cold_generation_busy` and `get_hydro_display_session` **stay physically
    in `apps/api/routes/hydro_display.py`**. No `hydro_display_db.py` is created.
    Rationale: a facade re-export of a `global`-mutated name or of an
    `lru_cache`'d factory that resolves `create_engine` from its own globals is an
    independent binding, so the existing patches in
    `tests/test_node27_connection_attribution.py` would silently stop biting; and
    moving `_engine` would move the attributed engine root that
    `REGISTERED_COMPONENTS` / `DISPLAY_UNIT_CONNECT_CLOSURE` pin to
    `apps/api/routes/hydro_display.py` -> `nhms-display-api`. If the line budget
    cannot be met without moving this block, stop and report rather than moving
    it; the attribution root swap is a separate decision.
  - Stable Facade: every symbol any test or script imports or patches stays
    importable from `apps.api.routes.hydro_display`; the nine handler function
    names, the eight Pydantic class names (`Layer`, `ApiSuccessEnvelope`,
    `LayerListResponse`, `LayerValidTimes`, `LayerValidTimesResponse`,
    `DischargeCycle`, `DischargeCycles`, `DischargeCyclesResponse`), the
    `hydro-display` tag and all route paths unchanged.
  - Monkeypatch Contract: consumers that tests patch through stay on the facade
    (design Decision 1). A patched name with consumers in two modules is
    dual-patched, never assigned one arbitrary home. Any retargeted patch site is
    proved non-vacuous (Decision 3 oracle 2) and the proof is reported. Report the
    final disposition of all 19 patched names in Appendix A's table.
  - Registry Update: remove `apps/api/routes/hydro_display.py` from
    `.large-file-guard.json`; add every new module to
    `GUARDED_MODULE_CLOSURES` in `scripts/select_ci_tests.py` and update the
    closure-count test in `tests/test_select_ci_tests.py`; register non-engine
    new modules in the delegated whitelist of
    `tests/test_node27_connection_attribution_delegated.py` and the
    attribution map in `tests/test_node27_connection_attribution.py`.
  - Cycle Rule: an owner module never imports the facade. Resolve a cycle by
    keeping the consumer on the facade or by moving the shared constant into
    `hydro_display_constants.py`; a function-local
    `from apps.api.routes import hydro_display` is a body edit disguised as a move
    and is not allowed. Per Appendix B, `_require_display_ready` stays on the
    facade because it calls `_run_row`.
  - Logger Identity: the `#2030` budget warnings keep the logger name
    `apps.api.routes.hydro_display` after `_fetch_postgis_tile_bytes` moves (see
    Appendix C). Evidence required: a positive truncation assertion still observes
    the record, and a negative assertion still fails when a warning is forced.
  - Source-Text Update: repoint every `tests/`/`scripts/` assertion that reads
    `apps/api/routes/hydro_display.py` as text to the file now holding that SQL,
    and show each repointed assertion still matches non-empty text.
  - Verification: `uv run pytest -q tests/test_openapi_drift.py`;
    `uv run pytest -q tests/test_hydro_display_mvt_scaling.py tests/test_display_mvt_cold_admission.py tests/test_api_contract.py tests/test_node27_connection_attribution.py tests/test_node27_connection_attribution_delegated.py tests/test_select_ci_tests.py tests/test_migrations.py tests/test_sql_shape_helpers.py tests/test_direct_grid_display_cutover_flip.py tests/test_direct_grid_display_cutover_history.py tests/test_direct_grid_display_cutover_model_resolution.py tests/test_river_ts_read_path_surrogate_keys.py tests/test_river_ts_text_identity_cleanup.py tests/test_mvt_tile_generation_lock.py tests/test_display_publish_status_only.py tests/test_hhe_mvt_binding.py tests/test_api_errors_logging.py tests/test_precip_overlay.py`;
    `git diff origin/master -- openapi/nhms.v1.yaml` empty;
    `uv run ruff check .`; pure-move oracle output recorded.

- [x] 1.2 Split `apps/api/openapi_patching.py` (2203 lines) below the guard
      threshold (#2074 item 2).
  - Module/Scope: move `_patch_*` / `_*_parameter` families to owner modules;
    keep `openapi_patching.py` as a compatibility facade.
  - Stable Facade: `_patch_mvt_tile_openapi` and `_patch_pipeline_openapi` are
    re-exported by direct `from ... import` so `is` identity against
    `apps/api/main.py`'s re-exports holds; `patch_openapi_schema`,
    `_publish_security_boundary` and the 13 `main.py` re-exports keep resolving.
  - Ordering Contract: patch application order and `_finalize_openapi_schema`
    timing unchanged.
  - Registry Update: remove `apps/api/openapi_patching.py` from
    `.large-file-guard.json`; add new modules to the
    `PathTestRule("apps/api/openapi_patching.py", ...)` coverage in
    `scripts/select_ci_tests.py` without introducing a duplicate pattern.
  - Verification: `uv run pytest -q tests/test_openapi_drift.py tests/test_slurm_gateway_openapi_security.py tests/test_api_contract.py tests/test_select_ci_tests.py`;
    `git diff origin/master -- openapi/nhms.v1.yaml` empty; `uv run ruff check .`.

## 2. Test-suite splits

- [x] 2.1 Split `tests/test_hydro_display_mvt_scaling.py` (4875 lines, 61+ test
      functions) into topic partitions (#2074 item 1).
  - Module/Scope: partition by topic; every partition <= 1000 lines with
    headroom. Shared fixtures go to a helper module, not a duplicated copy.
  - Delivered: 4888 lines / 210 cases -> nine partitions (188-718 lines each)
    plus one shared support module `tests/hydro_display_mvt_helpers.py` (487).
    The base path `tests/test_hydro_display_mvt_scaling.py` SURVIVES as the
    tile-SQL/`#2030`-budget partition because three registries pin that literal
    string (`openspec/specs/ci-contract-baseline/spec.md:295,1160`, the
    `infra/systemd/nhms-display-api.service` rule whose only reader
    `test_systemd_workers_receive_shared_file_cache_default` stayed there, and
    the `services/tiles/mvt.py` anchor in `GUARDED_MODULE_CLOSURES`), so no
    deployed-spec delta is owed. Pure-move oracle: with imports and module
    docstrings stripped, the 4079 non-blank body lines of the ten new files are a
    multiset-identical partition of the original's 4079 — zero residue either
    way. Test identity: 210 == 210 collected, and the sorted `name[param]` id
    lists are byte-identical (the file prefix necessarily moves, nothing else).
  - Single-use helpers stayed with their tests rather than moving to the shared
    module: `_final_select_text` (base-only), `_national_sql_sites` /
    `_national_digest_ranked_subquery` / `_VALID_TIME_*` (national_sql-only),
    `_recording_digest_calls`, `_cycle_days_ago`, the `_LegacyRunRouteSession`
    family and the `#2087` session subclasses. One consumer means no drift
    hazard, and the landmark slices keep their preconditions inside the same
    function as the slice they guard. The autouse
    `_keep_fixed_instant_fixtures_inside_the_cycle_lookback` DID move to the
    shared module and is imported by all nine partitions so its original
    module-wide reach is unchanged; disabling it in the shared module reds 15
    cases across three partitions (discovery / catalog / coverage_order), which
    is the live proof the import preserves autouse.
  - Assertion Integrity: each SQL landmark `.index()/.rindex()` slice migrates
    together with its non-empty and ordering preconditions
    (`cte_start < cte_end < probe_start < probe_end`); no slice assertion may
    degrade to comparing empty strings. Per-station individual slicing is kept —
    a `count(...) >= 2` style aggregate is not an acceptable substitute.
  - Registry Update: remove the entry from `.large-file-guard.json`; replace the
    single target with every collectible partition in the
    `services/tiles/mvt.py` and `hydro_display` `PathTestRule` entries of
    `scripts/select_ci_tests.py`; keep the importer-closure guard green.
  - Registry Update delivered: `.large-file-guard.json` entry removed, no new
    entry added. In `scripts/select_ci_tests.py` the single target became
    `HYDRO_DISPLAY_MVT_SCALING_TESTS` (all nine) on the `services/tiles/mvt.py`
    rule and `HYDRO_DISPLAY_MVT_SCALING_FACADE_TESTS` (seven) on the
    `apps/api/routes/hydro_display*.py` rule — the two derived closures differ
    because `..._discovery.py` and `..._national_sql.py` import no
    `apps.api.routes` module; both are still in the PR lane through the mvt rule.
    A new `SUPPORT_MODULE_TEST_RULES` entry routes
    `tests/hydro_display_mvt_helpers.py` to all nine (without it a
    fixture-only diff collapses to the selector meta-guard), with the matching
    `SUPPORT_MODULE_ROUTING_ANCHORS` pair. Two capability rules were narrowed
    instead of widened, each per its own measured-provenance comment: the
    `apps/api/openapi_patching*.py` rule takes only the two partitions holding
    the `test_runtime_openapi_documents_*` pins (#2211's criterion is "reads the
    patched document"), and `tests/river_ts_template_registry.py` takes only
    `..._instants.py`, its one importer. The
    `infra/systemd/nhms-display-api.service` rule is unchanged.
    `GUARDED_MODULE_CLOSURES` stays at 6: the partitions are test files, not
    guarded modules, and the `services/tiles/mvt.py` anchor still names a real
    direct importer because the base partition survives.
  - Verification: `uv run pytest -q tests/test_hydro_display_mvt_scaling*.py`
    (all partitions collected, test count >= pre-split count);
    `uv run pytest -q tests/test_select_ci_tests.py`; `uv run ruff check .`.
  - Verification run (local, 2026-09-20): `--collect-only` 210 collected;
    `pytest -q tests/test_hydro_display_mvt_scaling*.py` 210 passed;
    `pytest -q tests/test_select_ci_tests.py` 736 passed;
    `pytest -q` over the nine partitions plus `test_openapi_drift`,
    `test_display_mvt_cold_admission`, `test_precip_overlay`, `test_api_contract`,
    `test_mvt_tile_generation_lock`, `test_hhe_mvt_binding`,
    `test_openapi_31_contract` 415 passed; ruff over every tracked `*.py` green;
    `openspec validate --strict --no-interactive` green. Non-vacuity receipts:
    emptying the `preeligible` slice in the base partition reds
    `assert 'source_coordinate_count <= :feature_coordinate_limit' in ''`;
    collapsing `cte_end` onto `cte_start` reds `assert 164 < 164`; widening
    `_national_digest_ranked_subquery` to the whole statement reds its
    `WHERE rn = 1` exclusion. Logger receipt: a forced
    `logger.warning` in `hydro_display_postgis._fetch_postgis_tile_bytes` reds
    BOTH negative caplog assertions (`_truncation_records` in the base partition,
    `_blanked_records` in the feature-budget partition) under the unchanged
    `apps.api.routes.hydro_display` logger name, and was reverted
    (`git status --porcelain apps/` empty).

- [x] 2.2 Split `tests/test_api_contract.py` (2114 lines, 38 test functions,
      6 mock store classes) into partitions (#2074 item 3).
  - Module/Scope: mock store classes are the natural seam; partitions <= 1000
    lines with headroom.
  - Delivered: 2132 lines / 38 cases -> three collectible partitions plus one
    shared support module. `tests/test_api_contract.py` (524) SURVIVES as the
    contract-artefact partition (committed `openapi/nhms.v1.yaml` vs runtime
    `app.openapi()` vs generated `apps/frontend/src/api/types.ts`, display
    control plane, tile static/runtime pins) because four registries pin that
    literal string: `OPENAPI_CONTRACT_TESTS` / `PRECIP_SURFACE_TESTS` / the
    `services/tiles/mvt.py` and `apps/api/routes/hydro_display*.py` rules in
    `scripts/select_ci_tests.py`, the
    `apps/api/routes/hydro_display_catalog.py` row of
    `GUARDED_MODULE_CLOSURES`, `openspec/specs/api-contract-convergence/spec.md`
    (queue-depth static-vs-runtime code sets) and
    `tests/test_openapi_31_contract.py`'s function-body import of
    `OPENAPI_TYPESCRIPT_PACKAGE`. `tests/test_api_contract_pipeline_ops.py`
    (632) holds the control-plane / job-lifecycle routes;
    `tests/test_api_contract_resources.py` (478) the registry-backed resource
    routes; `tests/api_contract_helpers.py` (618) is the single home of the six
    mock stores, the retry gateway double and the eight private assertion
    helpers (`_parameter_names` / `_resolve_parameter` included), never
    duplicated. `_OversizedRiverSegmentStore` stayed with its base class
    `_ModelRegistryStore`. The pass-A rebinds (`hydro_display_catalog` +
    both facade names) moved nowhere: they are in the retained base file.
  - Pure-move oracle: with module docstrings and module-level import statements
    stripped, the 1905 non-blank body lines of the four new files are a
    multiset-identical partition of the pre-change file's 1905 — zero residue
    either way. Test identity: 38 == 38 collected and the sorted case-name lists
    are byte-identical (no parametrization exists in this corpus, so the only id
    change is the file prefix).
  - Fixture reachability: this corpus has NO pytest fixture — every case writes
    `app.dependency_overrides[...]` directly under `try/finally` — so the oracle
    reduces to isolated-run evidence. Each partition run alone passes its whole
    count with zero skips (11 / 12 / 15), and the glob run reports 38 passed;
    the split reverses the old execution order (the artefact cases ran last,
    now first) and nothing leaks across it.
  - Registry Update: remove the entry from `.large-file-guard.json`; replace the
    single target with every partition in `OPENAPI_CONTRACT_TESTS` and in the
    `services/tiles/mvt.py` / `hydro_display` `PathTestRule` entries.
  - Registry Update delivered: `.large-file-guard.json` entry removed, no new
    entry added. In `scripts/select_ci_tests.py` a new `API_CONTRACT_TESTS`
    tuple (all three) replaces the single target in `OPENAPI_CONTRACT_TESTS`
    (all three load `openapi/nhms.v1.yaml` at assertion level) and in the broad
    `apps/api/**` rule (the only rule that runs the resource-route contracts on
    a diff to the routes serving them — naming only the base path would drop 27
    of 38 cases from the PR lane). Three rules are deliberately NOT widened,
    each with its measured provenance recorded in place: the
    `services/tiles/mvt.py` and `apps/api/routes/hydro_display*.py` rules
    (only the base partition imports that family, which is what the
    guarded-module closure guard derives), `apps/api/openapi_patching*.py` (the
    #2211 criterion is "reads the patched runtime document"; all four
    `app.openapi()` call sites are in the base partition) and the shared
    `PRECIP_SURFACE_TESTS` tuple (only the base partition holds a
    whole-document comparison, so widening it would make every precip-tree diff
    pay for 27 cases that cannot red on it). A new
    `SUPPORT_MODULE_TEST_RULES` entry routes `tests/api_contract_helpers.py` to
    its derived closure — the three partitions plus
    `tests/test_openapi_response_conformance.py`, whose module-scope import of
    `_ModelRegistryStore` / `_RunStore` was repointed from the former monolith —
    with the matching `SUPPORT_MODULE_ROUTING_ANCHORS` pair. In
    `tests/test_select_ci_tests.py` the two `INTENTIONAL_RULE_GAP_EXCLUSIONS`
    rows for `workers/data_adapters/base.py` and
    `services/orchestrator/production_contract.py` follow their module-scope
    importer to `..._pipeline_ops.py` (the stale-exclusion guard reds otherwise),
    five exact-set selection pins gained the two new partitions as literals, and
    the row-5b explicit-target count moved 3 -> 5.
  - Spec delta owed and paid: widening the broad `apps/api/**` rule changes
    three exact-set scenarios of the DEPLOYED `ci-contract-baseline` spec
    (`apps/api/routes/precip.py`, `apps/api/route_registry.py`,
    `apps/api/main.py`), so this change now carries a
    `specs/ci-contract-baseline/spec.md` delta with both owning requirements
    MODIFIED. The `services/precip/**` tree scenario is unchanged, which is the
    live proof that `PRECIP_SURFACE_TESTS` itself was not widened.
  - Verification: `uv run pytest -q tests/test_api_contract*.py` (test count
    >= pre-split count); `uv run pytest -q tests/test_openapi_drift.py tests/test_select_ci_tests.py`;
    `uv run ruff check .`.
  - Verification run (local, 2026-09-20): `--collect-only` 38 collected;
    `pytest -q tests/test_api_contract*.py` 38 passed; `pytest -q
    tests/test_select_ci_tests.py` 737 passed; `pytest -q` over the three
    partitions plus `test_openapi_drift`, `test_openapi_31_contract`,
    `test_openapi_response_conformance`, `test_monitoring_api`, `test_api`,
    `test_pipeline_ops_identity_envelope` 263 passed; ruff over every tracked
    `*.py` plus the new files green; `git diff origin/master --
    openapi/nhms.v1.yaml` empty; `openspec validate
    split-guarded-display-modules --strict --no-interactive` valid; the
    `large-file-guard` hook exits 0 on the staged commit with no exemption.

## 3. Frontend splits

- [x] 3.1 Split `apps/frontend/src/lib/m11/overviewDataContracts.ts` (1112) and
      `apps/frontend/src/stores/overviewData.ts` (1268) (#2102).
  - Module/Scope: submodules plus a barrel at each original path; export names
    and signatures unchanged.
  - Stable Facade: all 26 + 18 importers unchanged; zero test-file changes; no
    `export default` exists in either file, so `export *` covers the surface.
  - Store State: keep each piece of module-level mutable state in
    `stores/overviewData.ts` (`cache`, `overviewLoads`, the request nonces) in the
    same submodule as its mutators, or use an explicit re-export list, so the
    barrel does not newly expose internal state as a named export.
  - Registry Update: remove both entries from `.large-file-guard.json`; keep
    `apps/frontend/src/api/types.ts` exempt.
  - Verification: `cd apps/frontend && pnpm exec tsc --noEmit -p tsconfig.app.json && pnpm test && pnpm build`;
    `git diff --stat origin/master -- 'apps/frontend/src/**/__tests__/**' 'apps/frontend/src/**/*.test.ts*'`
    empty.

## 4. Closure evidence

- [x] 4.1 Guard closure: `wc -l` shows every touched and new file <= 1000; for
      each of the six paths `grep -c "<path>" .large-file-guard.json` == 0; no
      new path added to `exclude`; `apps/frontend/src/api/types.ts` still
      present. A commit touching each family passes the `large-file-guard` hook
      with no exemption.
- [x] 4.2 Local gate: `uv run ruff check .` green;
      `openspec validate split-guarded-display-modules --strict --no-interactive`
      green; frontend `tsc` + `pnpm test` + `pnpm build` green.
- [x] 4.3 node-27 live receipt (the oracle for display changes, per
      `docs/runbooks/node-27-bringup-checklist.md` C1-C4): real-DB pytest over
      the affected suites, plus `curl` on `/api/v1/layers` and at least one MVT
      tile route showing unchanged response shape.
- [ ] 4.4 CI on the pushed head is green, including the targeted-test lane
      selecting the new partitions rather than degrading to `--collect-only`.

### node-27 live receipt (task 4.3), head 9fd60807ca6289b9a3f60b54f87800ef00db641d

Produced from an isolated `git worktree` at `/home/nwm/verify-2526` on node-27, served on spare
port 8097 by `/home/nwm/NWM/.venv/bin/python` (3.11.15) against the active local PG `:55432`.
The production `nhms-display-api.service` was never restarted and the shared `/home/nwm/NWM`
checkout stayed on `master` and clean throughout (production PIDs 3270965/3270971/3270972
unchanged before and after). Worktree, spare-port process and temp cache removed afterwards.

Real-DB pytest on node-27:
- `tests/test_api_contract{,_pipeline_ops,_resources}.py tests/test_openapi_drift.py tests/test_openapi_31_contract.py` -> **71 passed**
- the 9 `tests/test_hydro_display_mvt_scaling*.py` partitions + `test_display_mvt_cold_admission.py`
  + `test_mvt_tile_generation_lock.py` + `test_node27_connection_attribution{,_delegated}.py`
  + `test_precip_overlay.py` -> **478 passed**

Live HTTP, new code (:8097) vs production master (:8080):
- `GET /api/v1/layers` -> 200 / 6073 bytes on both; structural diff of the two JSON bodies has
  **exactly one differing leaf, `request_id`** (a per-request UUID). 4 layers: discharge,
  river-network, met-stations, precip.
- `GET /api/v1/tiles/river-network-national/3/6/3.pbf` -> 200, `application/x-protobuf`,
  248347 bytes, **byte-identical** between new code and production.
- `GET /api/v1/tiles/river-network-national/5/26/12.pbf` -> 200, 148784 bytes,
  **byte-identical**; 1.24s on the new instance (cold-generated, so the moved PostGIS path
  really executed) vs 0.018s cached on production.
- `GET /api/v1/layers/discharge/cycles` with no query params -> 422 on both, error envelope
  identical ignoring `request_id`, `rejected_value` still `[redacted]`.
- Verification instance log: 9 lines, no traceback; the single WARNING is the deliberate 422 above.
