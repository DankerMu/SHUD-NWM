# Tasks — test-oracle-lane-hygiene

Fixture level: standard. Risk packs:
- test-evidence (#2490 must be a pure move; the oracle is the collect-only diff)
- ops-runbook (node-27 lane recipe)

## Must preserve

- Every original test node collected exactly once: no test name, parameter ID, marker or assertion changes, and no new skips.
- Selector semantics: an owner path that selected an old file selects all of its parts; the #1447 PR-lane exclusion still applies.
- Without `NHMS_RUN_GRIB=1`, the grib tests still skip. The `integration`/`e2e` gates are unchanged.
- No production code change; node-22 is not written to.

## 1. #2490 test split

- [x] 1.1 Save master's `--collect-only -q` node lists in three configurations (default, `NHMS_RUN_INTEGRATION=1`, `-m integration`) to `.workplans/`.
- [x] 1.2 Split the three files into `test_*.py` suites plus helper modules, every file under 1000 lines, `pytestmark` kept.
- [x] 1.3 Remove the three exemptions from `.large-file-guard.json`.
- [x] 1.4 Update the `scripts/select_ci_tests.py` routes and `tests/test_select_ci_tests.py` per D1, with no support-module rules for the `*integration*` helpers. `tests/test_select_ci_tests.py` passes in full.
- [x] 1.5 Collected-set oracle (D1): the multiset of old-path nodes on master equals the multiset of part-union nodes on the branch, in all three configurations.
- [x] 1.6 Local run oracle: `pytest -q -rs` passed and skipped counts are equal between master (the old three files) and the branch (the part set).

## 2. #2595 real_disk cycle

- [x] 2.1 Add `latest_complete_cycle` in `tests/object_store_forcing_real_disk_support.py` (D2), use it through a lazy module-scoped fixture, and derive every time from it; no hardcoded real date remains in the source.
- [x] 2.2 Add local unit tests in `tests/test_object_store_forcing_real_disk_support.py`: newest common cycle wins, incomplete cycle skipped, a too-fresh file excluded, empty intersection fails with the diagnostic.
- [ ] 2.3 On node-27, run the suite read-only as `nhms_display_ro`, with `DATABASE_URL`, `OBJECT_STORE_ROOT`, `NHMS_RUN_E2E=1` and `NHMS_RUN_REAL_DISK=1` set, with `-rA` (`-q` drops the fixture's print): the result must be **4 passed, 0 skipped**, and the receipt records the chosen cycle.

## 3. #2594 GRIB lane

- [x] 3.1 Add the GRIB runtime injection to the runbook lane and record the version-match fact.
- [x] 3.2 Add the conftest preflight and a test for the missing-library diagnostic.
- [ ] 3.3 On node-27, run `-m grib` using the lane: all 8 grib tests run with no `Cannot find the ecCodes library`. Write a receipt.

## 4. #2615 worktree lane

- [x] 4.1 Write the runbook recipe: PATH, PYTHONPATH, TMPDIR, the `packages.__file__` assertion, and why PYTHONPATH must stay.
- [x] 4.2 Decouple tests A and B from the environment. A exports `PYTHONPATH` itself so dropping the stripping goes red; B has `_validator()` unit tests for the PATH hit, the beside-interpreter fallback, no `.resolve()`, and the not-found message.
- [ ] 4.3 On node-27, run the two files with the recipe and all pass. A passes with `PYTHONPATH` exported; B passes with no `.venv/bin` on `PATH`. The local `uv run pytest` also passes.

## 5. Closing

- [ ] 5.1 On node-27, run the full set of touched and split files with the worktree recipe (including `-m integration` against a throwaway DB).
- [ ] 5.2 Write back measured results as comments on the #1632 receipt issue: the 8 grib tests and the 4 real_disk tests.

## Evidence Floor

- Collected-set oracle empty (3 configurations); local passed/skipped counts equal; every file < 1000 lines; the `git commit` hook exits 0.
- node-27 `-m integration` (throwaway DB): the split integration files give the same passed count on master and on the branch.
- node-27: real_disk **4 passed, 0 skipped** with the cycle recorded; 8 grib tests run with no ecCodes load error; the A and B recipe runs pass.
- Local: `uv run ruff check .`; `uv run pytest -q` over every touched and split file, the whole of `tests/test_select_ci_tests.py`, `tests/test_node22_entrypoint_invariant.py`, the new support and preflight tests, and every suite that imports `tests.conftest`; `openspec validate test-oracle-lane-hygiene --strict --no-interactive`.
