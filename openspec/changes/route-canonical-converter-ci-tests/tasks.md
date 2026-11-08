## Risk packs

- **Config (selector rules) — selected.** One new non-stop `PathTestRule` with an explicit tuple. Covered by the dry-run pins.
- **Test infrastructure — selected.** `DIRECTORY_RULE_AUDIT_PATHS` gains the directory; every reported importer pair gets a reasoned exclusion. Covered by `tests/test_select_ci_tests.py`.
- **Legacy compatibility — selected.** Converter-only diffs no longer fall back to `CORE_SMOKE_TESTS`; the two CORE_SMOKE importers are dispositioned (`runtime-budget`, `edge-consumer`). Covered by the exclusion entries and the converter-only pin.
- **Not selected:** public API, schema, file IO, auth, concurrency, resource limits, release, documentation (the rule comment is the record).

## Evidence Floor

- `uv run ruff check .`
- `uv run pytest -q tests/test_select_ci_tests.py`
- `printf 'workers/canonical_converter/converter.py\n' | uv run python scripts/select_ci_tests.py` (and the same for `grib_packing.py`, `cli.py`; `--changed-file` takes a list file, not a path) output recorded in the PR body

## Tasks

- [x] 1.1 Add the non-stop `workers/canonical_converter/**` `PathTestRule` with the explicit eight-suite tuple and a comment citing #3093 / #1711, the edge-consumer convention, the explicit-tuple anchoring, and `test_grib_packing_reader` being collection-only in the PR lane.
- [x] 1.2 Add `workers/canonical_converter` to `DIRECTORY_RULE_AUDIT_PATHS` (comment count ten → eleven) and close the gap set listed in proposal.md in `INTENTIONAL_RULE_GAP_EXCLUSIONS`, plus any further pair the guard reports, each with a reason.
- [x] 1.3 Pin the three dry-run selections (converter, grib_packing, cli) in `tests/test_select_ci_tests.py`.
- [x] 2.1 Ruff and `tests/test_select_ci_tests.py` green locally; record the three dry-run outputs.
