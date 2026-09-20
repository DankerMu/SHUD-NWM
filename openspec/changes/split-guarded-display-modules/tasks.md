## 0. Risk pack pass (canonical vocabulary)

- [ ] 0.1 Record the pack verdicts below and keep each mapped item satisfied.
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

- [ ] 2.1 Split `tests/test_hydro_display_mvt_scaling.py` (4875 lines, 61+ test
      functions) into topic partitions (#2074 item 1).
  - Module/Scope: partition by topic; every partition <= 1000 lines with
    headroom. Shared fixtures go to a helper module, not a duplicated copy.
  - Assertion Integrity: each SQL landmark `.index()/.rindex()` slice migrates
    together with its non-empty and ordering preconditions
    (`cte_start < cte_end < probe_start < probe_end`); no slice assertion may
    degrade to comparing empty strings. Per-station individual slicing is kept —
    a `count(...) >= 2` style aggregate is not an acceptable substitute.
  - Registry Update: remove the entry from `.large-file-guard.json`; replace the
    single target with every collectible partition in the
    `services/tiles/mvt.py` and `hydro_display` `PathTestRule` entries of
    `scripts/select_ci_tests.py`; keep the importer-closure guard green.
  - Verification: `uv run pytest -q tests/test_hydro_display_mvt_scaling*.py`
    (all partitions collected, test count >= pre-split count);
    `uv run pytest -q tests/test_select_ci_tests.py`; `uv run ruff check .`.

- [ ] 2.2 Split `tests/test_api_contract.py` (2114 lines, 38 test functions,
      6 mock store classes) into partitions (#2074 item 3).
  - Module/Scope: mock store classes are the natural seam; partitions <= 1000
    lines with headroom.
  - Registry Update: remove the entry from `.large-file-guard.json`; replace the
    single target with every partition in `OPENAPI_CONTRACT_TESTS` and in the
    `services/tiles/mvt.py` / `hydro_display` `PathTestRule` entries.
  - Verification: `uv run pytest -q tests/test_api_contract*.py` (test count
    >= pre-split count); `uv run pytest -q tests/test_openapi_drift.py tests/test_select_ci_tests.py`;
    `uv run ruff check .`.

## 3. Frontend splits

- [ ] 3.1 Split `apps/frontend/src/lib/m11/overviewDataContracts.ts` (1112) and
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

- [ ] 4.1 Guard closure: `wc -l` shows every touched and new file <= 1000; for
      each of the six paths `grep -c "<path>" .large-file-guard.json` == 0; no
      new path added to `exclude`; `apps/frontend/src/api/types.ts` still
      present. A commit touching each family passes the `large-file-guard` hook
      with no exemption.
- [ ] 4.2 Local gate: `uv run ruff check .` green;
      `openspec validate split-guarded-display-modules --strict --no-interactive`
      green; frontend `tsc` + `pnpm test` + `pnpm build` green.
- [ ] 4.3 node-27 live receipt (the oracle for display changes, per
      `docs/runbooks/node-27-bringup-checklist.md` C1-C4): real-DB pytest over
      the affected suites, plus `curl` on `/api/v1/layers` and at least one MVT
      tile route showing unchanged response shape.
- [ ] 4.4 CI on the pushed head is green, including the targeted-test lane
      selecting the new partitions rather than degrading to `--collect-only`.
