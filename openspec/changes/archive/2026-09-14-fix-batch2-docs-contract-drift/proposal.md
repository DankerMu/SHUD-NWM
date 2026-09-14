## Why

Batch 2 closes eight docs/spec contract-drift issues. Each is a living document or promoted
spec asserting something the code, migrations, CI, or test inventory contradicts; several
recipes are false-green (skip with rc 0, ruff no-op on `.md`) or fail outright (archived
change names, invalid enum literal).

- #2059 real-Basins import smoke recipe lacks `NHMS_RUN_INTEGRATION` / `NHMS_INTEGRATION_DATABASE_URL` → skips with rc 0.
- #2055 runbook + `basins-registry-import` spec point at a retired nodeid (moved to `tests/test_basins_registry_import_qhh.py`).
- #2047 four docs freeze `hydro.run_status` at the original snapshot (`frequency_done` retired, `pending` missing); one runbook SQL fails on a fresh DB.
- #2155 `basins-registry-import` spec says the output-river backfill family SHALL be removed, yet it runs by default and is contractualized by the same spec (`Output-river geometry backfill writes only the target network version`) and by `mvt-tile-contract`.
- #1936 ~16 `openspec validate <archived-change>` recipes return `Unknown item`.
- #1935 five `ruff check` commands pass `.md` paths (silent no-op).
- #1862 `failed-basin-retry.md` inverts the D3 "unique" definition.
- #2234 `ci-contract-baseline` spec asserts selector behavior current tests disprove.

Decisions (derived from issue recommendations + code evidence, no product call needed):
#2047 drop `frequency_done` stage, six stages matching `apps/frontend/src/lib/constants.ts`;
#2155 keep the live function family, correct the spec (remove the four output-river tokens
from the SHALL-remove list and the dead grep scenario, fix the stale `gis/seg.shp` reason);
#1935 annotate `progress.md` as outside markdownlint scope (no CI change);
#2234 correct spec text to the tested behavior (direction A); #2059 canonical three-variable
recipe (no compat flag).

## What Changes

- Docs: `docs/VALIDATION.md`, `docs/validation/production-closure.md`, `README.md`, `progress.md`,
  `docs/runbooks/{current-production-ops,failed-basin-retry,forcing-copyback-backfill,qhh-mvp-*}.md`,
  `docs/spec/{01_architecture_and_flow,03_database_design}.md`, `docs/appendices/C_database_schema_draft.md`,
  `docs/modules/14_tile_publication_service_{spec,design}.md`.
- Specs (MODIFIED deltas): `basins-registry-import`, `ci-contract-baseline`.
- Test: `tests/test_select_ci_tests.py` static recipe meta-test tightened to require the integration gate variables (#2059).

`design.md` exempt (compact fixture).

## Impact

No runtime code, schema, CI workflow, or production config change.
