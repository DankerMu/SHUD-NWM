## Why

A PR that changes only `workers/canonical_converter/converter.py` (de-accumulation, unit conversion, readiness) runs only `CORE_SMOKE_TESTS` in the targeted "Unit Tests" lane (#3093). `scripts/select_ci_tests_rules.py` has no `workers/canonical_converter/**` row, no `tests/test_converter.py` / `tests/test_cli.py` exists for the same-name derivation, so the path falls to `unknown_backend_path` (`scripts/select_ci_tests.py:456-459`). The converter/adapter suites never run on such a PR; a regression in the code #3090 just changed surfaces only on the post-merge master shards. The directory is also missing from `DIRECTORY_RULE_AUDIT_PATHS`, so the importer-gap guard cannot see the gap.

## What Changes

- **New non-stop directory rule** in `scripts/select_ci_tests_rules.py`: `workers/canonical_converter/**` selects an explicit tuple of the package's own suites:
  `tests/test_canonical_converter.py`, `tests/test_ifs_canonical.py`, `tests/test_era5_canonical.py`, `tests/test_e2e_ifs.py`, `tests/test_ifs_adapter.py`, `tests/test_gfs_adapter.py`, `tests/test_era5_adapter.py`, `tests/test_grib_packing_reader.py`.
  - Same convention as the #1711 `workers/mapping_builder/**` row (`rules.py:1652-1680`): the rule carries only the package's suites; importers outside it are `edge-consumer`. Unlike #1711 the suite set cannot be derived from a tree glob, so it is an explicit tuple anchored by dry-run pins, with no tree-derived equality assertion.
  - The same-name derivation still adds `tests/test_grib_packing.py` for `grib_packing.py`.
  - `tests/test_grib_packing_reader.py`: all 3 tests are `@pytest.mark.grib` and skip without `NHMS_RUN_GRIB`, so in the PR lane it is a collection/import check only (3 collected / 3 skipped / 0 passed). It stays in the tuple for that reason, which the rule comment states; `tests/test_e2e_ifs.py` has 2 non-grib tests that execute.
- **CORE_SMOKE fallback is lost for these paths.** A matching rule sets `matched=True`, so a converter-only diff no longer selects `CORE_SMOKE_TESTS`. Selection is replaced, not only grown. The two CORE_SMOKE members that statically import converter symbols are dispositioned explicitly:
  - `(converter.py, tests/test_orchestration_chain.py)`: `runtime-budget`, reusing the 596 s measurement at `tests/test_select_ci_tests.py:11807`;
  - `(converter.py, tests/test_production_scheduler.py)`: `edge-consumer`, owned by the `services/orchestrator/**` rule (precedents `:11911`, `:11920`).
  Readiness behaviour stays covered by `tests/test_canonical_converter.py` and `tests/test_ifs_canonical.py`.
- **Audit coverage:** `workers/canonical_converter` joins `DIRECTORY_RULE_AUDIT_PATHS` (update the "TEN audited directory paths" comment at `:11623` to eleven). The guard derives gaps only from module-level `ast.Import`/`ImportFrom` (`:5853-5888`), so the five `importlib.import_module` suites and the function-body import in `tests/test_era5_adapter.py:398` are invisible to it; those edges are anchored only by the explicit tuple and the pins. The deterministic gap set to close in `INTENTIONAL_RULE_GAP_EXCLUSIONS`:
  - `converter.py` × `tests/test_e2e.py`, `tests/test_ifs_forecast_integration.py` (selected by `rules.py:1560/1562`), `tests/test_scheduler_refresh_catalog_derivation.py` (`SCHEDULER_REFRESH_TESTS`, `rules.py:2205`): `edge-consumer`; plus the two pairs above.
  - `__init__.py` and `cli.py` × `tests/test_slurm_array_contract.py`: `edge-consumer`, same reason as the five existing pairs (`:11747-11752`).
  - `__init__.py` and `cli.py` × `tests/test_source_identity.py`: `edge-consumer`, owned by `workers/forcing_producer/**` (`rules.py:1554`, precedent `:11891`).
  Any further pair the guard reports when run is closed the same way, with a reason.
- **Pins** in `tests/test_select_ci_tests.py` for three dry-runs:
  - converter-only: includes the eight suites;
  - `grib_packing.py`-only: includes `tests/test_grib_packing.py` and `tests/test_grib_packing_reader.py`;
  - `cli.py`-only: includes the eight suites and does not include `tests/test_slurm_array_contract.py` or `tests/test_source_identity.py`.

## Triage

```text
Issue type: ci / test routing (tech-debt)
Fixture level: standard
Upstream suggested level: absent
Blast radius: PR lane only. Over-selection costs ~7 s per converter PR; the CORE_SMOKE members dropped for converter-only diffs lose their incidental readiness coverage (covered by the converter suites instead); no production behaviour
Selected risk packs: Config (selector rules); Test infrastructure (meta-suite guards); Legacy compatibility (CORE_SMOKE fallback replaced)
Evidence floor: uv run ruff check . ; uv run pytest -q tests/test_select_ci_tests.py ; dry-run selections for converter.py / grib_packing.py / cli.py
```

## Impact

- **Code:** `scripts/select_ci_tests_rules.py` only.
- **Tests:** `tests/test_select_ci_tests.py` (`DIRECTORY_RULE_AUDIT_PATHS`, its comment, exclusions, routing pins).
- **Out of scope:** converter behaviour; generalising importer closure to source files; `CORE_SMOKE_TESTS` membership; changing the `importlib.import_module` style in the converter suites; helper modules (`tests/publish_registry_helpers.py`, `tests/qhh_production_bootstrap_helpers.py`), which the guard does not track.
- **Spec:** `ci-test-selector-routing`.
