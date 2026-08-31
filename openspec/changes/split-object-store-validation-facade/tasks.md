## 1. Baseline Contract

- [x] 1.1 Persist baseline SHA, source and guard digests/line counts, all 159 non-dunder facade names, 123 callable signatures, eight dataclass field contracts, public owner/class identities, two importer paths, and the finite eight-callable dynamic seam table.
- [x] 1.2 Record baseline synthetic fixture 27-file tree digest, standalone CLI usage contract, result/evidence schema and file inventory, and local per-suite counts: object-store 110; Slurm 70; readiness 349/2; publication 87/1; registry 78/18; reingest 2/4; scheduler registry 59; selector 405.

## 2. Eight-Owner Physical Split

- [x] 2.1 Create exactly `object_store_validation_contracts.py`,
  `object_store_validation_path_safety.py`,
  `object_store_validation_fixture.py`,
  `object_store_validation_manifest.py`, `object_store_validation_runtime.py`,
  `object_store_validation_consumption.py`, and
  `object_store_validation_evidence.py`; reduce the historical module to a
  sub-1,000-line facade and keep every owner below 1,000 lines.
- [x] 2.2 Move complete responsibility closures without cleanup/refactor; keep the contracts/path dependency DAG acyclic, prohibit import-time facade back-imports, and preserve synthetic `.sp.riv`/`.sp.rivseg` plus every other fixture byte exactly.
- [x] 2.3 Keep `EvidenceWriter`, `ProductionObjectStoreConfig`,
  `validate_object_store`, click/argparse/`main` and dynamic coordinators in the
  facade or as facade wrappers. Inject only the eight finite callable seams
  through direct/transitive leaf consumers; plain re-export all other baseline
  helpers and retain shared module/class identity.

## 3. Compatibility and Oracle Closure

- [x] 3.1 Add tracked, ignored-artifact-independent compatibility tests for the 159 names, 123 signatures, eight dataclasses, public owner/class identities, exact eight-owner set/line limits and both fresh-process importers.
- [x] 3.2 Bind all eight facade callables to biting high-level tests covering stored verification, live registry import, inventory/migration/package raw output, evidence/fixture/runtime safe writes and standalone CLI dispatch; prove patches alter the real moved call path rather than only attribute presence.
- [x] 3.3 Compare the 27-file fixture digest and representative ready/blocked/error/redacted evidence, manifest/checksum, runtime budget/receipt and cleanup outputs; retain existing symlink/TOCTOU/non-regular/oversize/stale/tamper/collision oracles without assertion, skip, marker or fixture weakening.
- [x] 3.4 Run `python -m` usage and packaged click/argparse smokes plus production/readiness/Basins/scheduler import consumers; report every non-mechanical qualified lookup/body change as a deviation with a biting equivalence test.

## 4. Structural and Scope Gates

- [x] 4.1 Prove all eight production files are `<1000` lines, `.large-file-guard.json` is byte-identical with no added/removed exclusion, entropy has no new gate-eligible finding, and the normal commit hook accepts the staged change.
- [x] 4.2 Confirm the diff contains no #1903 validator/error/test/fixture-byte
  change, Basins package or test-corpus split, CI workflow/docs route change,
  schema/status/blocker/redaction behavior refactor, DB/frontend/Slurm
  scheduling/SHUD change, or dependency. Diagnosed minimal selector-route
  deviation: add `tests/test_object_store_validation_facade_contract.py` to
  the existing `services/production_closure/**` rule so the historical facade
  and seven split owners run their direct compatibility oracle.

## 5. Evidence Floor

- [x] 5.1 Run `uv run pytest -q`
  `tests/test_production_object_store_validation.py`
  `tests/test_production_slurm_validation.py`
  `tests/test_production_readiness_validation.py`
  `tests/test_basins_package_publication.py`
  `tests/test_basins_registry_import.py tests/test_basins_reingest.py`
  `tests/test_publish_scheduler_file_registry.py`; expect no failures and
  recorded local per-suite semantics, allowing only existing platform-specific
  skips.
- [x] 5.2 Run fresh standalone/packaged CLI and importer smokes,
  `uv run pytest -q tests/test_select_ci_tests.py` with the diagnosed minimal
  `services/production_closure/**` route addition covering the historical
  facade and all seven new owner paths, and `uv run pytest -q tests/`; expect
  complete regression success without oracle weakening.
- [x] 5.3 Run `uv run ruff check .`, entropy/large-file audit tests and script, strict single/all OpenSpec validation, changed Markdown lint and `git diff --check`; expect zero new violations.
- [ ] 5.4 On node-27, capture the same focused suites at baseline SHA and frozen implementation SHA in detached worktrees without changing the active checkout; compare per-suite Linux results and record a durable receipt. Node-22 is not required because no Slurm/SHUD scheduling/runtime behavior changes.
