# Design — selector-route-gaps-batch-ci1

Only `scripts/select_ci_tests.py` and `tests/test_select_ci_tests.py` change. Each area follows the idiom already in the code. Measured baseline on master 86e6a17d5: pipeline 15, station_set_flip 9, model_registry 13, forecast_store 36, redaction 11, source_identity 11, each sbatch template 0.

## D0 Closure-guard mode (shared mechanism for D1/D3)
- The closure guard for `GUARDED_MODULE_CLOSURES` (`tests/test_select_ci_tests.py`, around :5556-5585) currently always requires `direct | _one_hop_importer_tests(module)`.
- Add a parallel constant `DIRECT_ONLY_GUARDED_MODULES: frozenset[str]` next to it. For those modules the guard requires only the direct non-gated importer set. The three-tuple shape of `GUARDED_MODULE_CLOSURES` is not changed.
- **Decision rule.** A new module is direct-only when its one-hop adds ≥ 10 suites that are not direct importers, because the one-hop then mostly reaches unrelated consumers through incidental helper imports. Measured examples: `pipeline.py` one-hop adds 21, pulling in the whole hydro_display closure through `_ok`/router imports; `model_registry.py` adds 25, mostly scheduler suites through `services/orchestrator/scheduler.py`.
- The constant's comment records the measured one-hop size of each member.
- `station_set_flip.py` is measured first: if its one-hop adds < 10 suites it uses the full guard.
- **Function-gated importers.** The guard's importer derivation filters only file-level marks. A direct importer that is gated only at function level, such as `tests/test_basins_registry_import_db.py` (5 tests, all function-level `integration`), is included in the rule, costing a few skips. No new exemption ledger.
- **Pins to update.** Refresh the guard's own "direct UNION one hop" comment (around :5563-5570) and the domain-split comment (around :12186) to mention the direct-only mode. `assert len(GUARDED_MODULE_CLOSURES) == 6` (around :14265) and the "unchanged at 6" comment become 9. Refresh the stale texts: the cancel-route docstring (around :1147-1160) and the "four modules" comment (around :10472).

## D1 #2568 + #2576: `apps/api/routes/pipeline.py`
- **#2568.** Add `tests/test_retry.py` (whole file) to `pipeline.py`'s exact rule, with a comment naming it as the only HTTP oracle for `POST /runs/{run_id}/retry`.
- **#2576.** Add `pipeline.py` to `GUARDED_MODULE_CLOSURES` (anchor `tests/test_retry_cancel_consistency.py`) and to `DIRECT_ONLY_GUARDED_MODULES`. Complete the rule to the direct non-gated set, which is 9 suites.
- **Conflict with an existing pin.** The direct set includes `tests/test_openapi_31_contract.py`, which imports `pipeline` at module level. The existing peer pin `test_connection_attribution_tuple_peers_are_untouched_by_the_registry_split` asserts that no peer selects `_PRECIP_ONLY_VIA_OWNER_RULE`, and that list includes this suite, so the two cannot both hold. That pin was written to stop the *shared tuple* from widening, not to forbid a genuine direct import. Narrow its leak assertion to exclude suites the peer reaches through its own direct import. Keep the check against the shared tuple, add a comment giving the reason, and add a mutation test proving that widening the shared tuple still turns it red.
- The PR body records the before and after selection set and the local wall time of the added suites.

## D2 #2575: `infra/sbatch/*.sbatch`
- **Rule.** One fnmatch glob rule, `infra/sbatch/*.sbatch` (the selector matches with fnmatch).
- **Targets.** Every suite that reads or renders the real `infra/sbatch` templates, whether through a literal path or through the production default `template_dir`:
  - `test_analysis_pipeline`, `test_job_array`, `test_object_store_roots`, `test_orchestrator`, `test_real_slurm_gateway`, `test_slurm_array_contract`, `test_slurm_route_security_contract`, `test_slurm_route_contract`;
  - `test_production_slurm_validation`, which renders through `services/production_closure/slurm_validation.py`'s default;
  - `test_m24_gateway_proof`.
  
  The implementer confirms each one actually loads templates. `test_runtime_mode` and `test_two_node_docker_runtime` only use env strings and are excluded.
- `infra/sbatch/README.md` stays unrouted, because it is prose.
- **Pins.** Removing the rule turns a pin red. Every `.sbatch` template selects at least one suite. The selections for `scripts/run_qhh_cycle.sbatch` and `infra/systemd/nhms-node27-autopipe.timer` do not change.

## D3 #2612: `station_set_flip.py` and `model_registry.py`
- **model_registry routing.** The facade `model_registry.py` and every `model_registry_*.py` owner must keep identical selections; the #2617 facade parity pin (around :14402-14419) requires equality. So do **not** move the facade out of the tuple-expanded rule. Instead:
  - add one shared tuple `MODEL_REGISTRY_IMPORTER_TESTS`, the direct non-gated importer set of the facade;
  - merge that tuple into the rule that the connection-attribution store tuple expands to for the facade and the owner glob.
  
  Owners carry the store method bodies, so selecting those suites for owners is not over-selection.
  - `MODEL_REGISTRY_IMPORTER_TESTS` also lists both cutover-flip parts, `tests/test_direct_grid_display_cutover_flip_atomic.py` and `_mvt_set.py`. They reach `model_registry` only through `tests/direct_grid_display_cutover_flip_helpers.py`, which the guard cannot derive, and #2612 AC 2 requires them. Pin both on the facade and on one owner. `CONNECTION_ATTRIBUTION_STORE_PATHS` is only referenced in comments, which are updated if the text changes.
- **station_set_flip routing.** A new exact rule with its direct non-gated importers, including both cutover-flip parts and `test_direct_grid_display_cutover_history`.
- **The `_mvt_set` edge.** `tests/test_direct_grid_display_cutover_flip_mvt_set.py` reaches both owners only through `tests/direct_grid_display_cutover_flip_helpers.py`. The guard cannot derive that edge (helpers under `tests/` are outside `_tracked_non_test_modules`), so it gets an explicit pin.
- **Guard membership.**
  - `model_registry.py` goes into `GUARDED_MODULE_CLOSURES` (anchor `tests/test_variant_activation_cutover.py`) and into `DIRECT_ONLY_GUARDED_MODULES`.
  - `station_set_flip.py` goes into `GUARDED_MODULE_CLOSURES` (anchor `tests/test_direct_grid_display_cutover_history.py`), with its mode chosen by the D0 rule.
- Existing picks stay: CONN, both preservation halves.

## D4 #2624: preservation oracle for five owners
- **Rules.** Add both preservation halves to:
  - the `services/orchestrator/**` broad rule targets;
  - the `packages/common/redaction.py` rule;
  - a new exact rule for `packages/common/source_identity.py`.
- **Frozen pin.** Update the literal in `test_select_tests_keeps_broad_orchestrator_fallback_for_other_orchestrator_changes`. Its comment records the measured local wall time of the two halves.
- **Builder registry.** Add the five modules to `RESPONSE_MODEL_ORACLE_INDIRECT_BUILDERS`, and rewrite the decision comment that scoped the orchestrator three out.
- **Leg-removal test.** The single-leg-removal parametrisation (around :21238-21264) now asserts a builder **set** per leg. Removing the broad orchestrator leg exposes three builders at once. Every new leg must be covered, and removing it must turn `_preservation_gaps()` red.
- **Meta-guard wording.** Narrow the meta-guard's name or docstring to its real traversal: the oracle files' own top-level `packages`/`workers` imports plus the named indirect builders.

## D5 batch extra: `forecast_store.py` → `tests/test_river_ts_stats_harness_offline.py`
- Add the suite to `forecast_store.py`'s exact rule and update the exact-set pin `test_select_tests_maps_forecast_store_without_core_smoke_fallback`.
- Out of scope, report only: the other 8 non-gated direct importers `forecast_store` currently misses.
  - `test_api_contract_resources`;
  - `test_direct_grid_display_cutover_b4_leak`, `_history` and `_model_resolution`;
  - `test_e2e` and `test_e2e_ifs`;
  - `test_model_registry_evidence_only_boundary`;
  - `test_object_store_forcing`.

## Evidence
- A before and after selection table for every path above, with counts and key suites.
- Mutation red for every new rule leg and for the D0 mode.
  - Removing a direct importer from a direct-only rule turns the guard red.
  - A synthetic new importer turns the guard red. This reuses the existing guard-mutation idiom.
- `uv run pytest -q tests/test_select_ci_tests.py` fully green, and `uv run ruff check .` clean.
- The added suites run green locally, with wall time recorded.
