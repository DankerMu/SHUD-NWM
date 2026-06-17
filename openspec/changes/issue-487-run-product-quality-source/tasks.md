# Tasks

- [x] Inspect current migration state and decide whether to modify `000034` or
  add a follow-up migration with clear compatibility rationale.
- [x] Extend `flood.run_product_quality` with explicit quality fields and safe
  defaults.
- [x] Remove or avoid creation of NULL partial indexes on
  `flood.return_period_result` in the production migration path.
- [x] Add explicit quality dataclass/helper API in `packages/common/flood_quality.py`.
- [x] Preserve historical result-row backfill compatibility.
- [x] Ensure explicit unavailable quality rows are not deleted merely because
  source result rows are absent.
- [x] Validate `residual_blockers` entries preserve at least `code`, `state`,
  `quality_flag`, `residual_risk`, and `run_id`.
- [x] Add helper/read compatibility coverage for missing quality table/schema:
  flood product quality fails closed or degrades clearly, but q_down/readiness
  helpers are not marked failed by that absence.
- [x] Add/update migration tests for schema, idempotency, and index absence.
- [x] Add/update helper tests for all-no-curve, partial-curve, and historical
  backfill behavior.
- [x] Run focused verification:
  - `uv run --no-sync pytest -q tests/test_migrations.py tests/test_return_period.py`
  - plus any new/updated focused tests.
- [x] Run lint on touched Python files:
  - `uv run --no-sync ruff check <touched-python-files>`

## Evidence Mapping

- PostGIS / TimescaleDB behavior: migration test proving no new NULL partial
  indexes and idempotent table extension.
- DB schema / audit contract: tests asserting explicit fields persist and
  round-trip, including minimum `residual_blockers` audit keys.
- Shared helper behavior: helper tests for explicit write/read and legacy
  backfill.
- Backward compatibility: existing count fields remain populated and historical
  backfill tests pass.
- Published artifacts / q_down boundary: no API route switch in this issue;
  evidence is helper/read compatibility proving missing flood quality storage
  does not mark q_down unavailable.
