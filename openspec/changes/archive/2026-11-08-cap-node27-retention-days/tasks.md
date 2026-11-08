## Risk packs

- **Config — selected.** A ceiling for both periods. Covered by boundary tests (3650 accepted, 3651 and 1000000 blocked) for each variable and for `--retention-days`.
- **Error handling — selected.** Above-ceiling values become a preflight blocker with a written `preflight_blocked` summary and rc 2 instead of an `OverflowError` traceback. Covered by a test that runs `main` with a summary path and asserts the summary and rc.
- **Documentation — selected.** Runbook rollback and preflight bullets. Reviewed in the diff.
- **Not selected:** public API, schema (`ceiling` key only on the new `above_ceiling` blocker), file IO, auth, concurrency, resource limits, release, legacy compatibility (in-range values unchanged; covered by the existing suite).

## Evidence Floor

- `uv run ruff check .`
- `uv run pytest -q tests/test_node27_raw_retention.py`

## Tasks

- [x] 1.1 Add `RETENTION_DAYS_CEILING = 3650`; `above_ceiling` (with `ceiling`) in `_forcing_retention_days`, existing blockers unchanged; `retention_days` `above_ceiling` blocker in preflight.
- [x] 1.2 Tests: boundary 3650 / 3651 / 1000000 for `NODE27_RAW_RETENTION_FORCING_DAYS`, `NODE27_RAW_RETENTION_DAYS` and `--retention-days`; a `main` run with `1000000` writes a `preflight_blocked` summary, returns 2 and raises nothing; existing tests unchanged. `--retention-days` cases call `main([...])` directly (`_production_tick` fixes argv); raw-period cases `delenv` `NODE27_RAW_RETENTION_FORCING_DAYS`.
- [x] 1.3 Runbook: ceiling in the preflight bullet and the forcing-pause rollback instruction; the same reason list in `infra/env/node27-raw-retention.example`.
- [x] 2.1 Ruff and `tests/test_node27_raw_retention.py` green locally.
