## ADDED Requirements

### Requirement: node-27's applied migration ledger MUST cover every shipped migration before code that reads a new column is served

node-27 SHALL NOT serve display or ingest code that projects a column whose migration is absent from
`public.schema_migrations`; the pending migration set SHALL be applied first, in its own timer-stopped window.

Display and ingest code on node-27 is deployed by moving a checkout forward, not by a release artifact, so a
migration that ships in `db/migrations/` but is never applied leaves the database behind the code with no
mechanism that surfaces it. `services/tiles/mvt.py::national_discharge_source_version` and
`::national_river_network_source_version` project `core.river_network_version.geometry_generation`, and
`workers/model_registry/basins_registry_import.py::_backfill_output_segment_geometry` updates it inside the
import transaction — the read side fails on the next display restart, the write side on the next ingest tick,
neither waiting for the other.

The pending set SHALL be computed as `db/migrations/*.sql` minus `public.schema_migrations`, and applied with
`packages.common.migrate` connected as the `nhms` owner role, so the ledger is written; `psql -f` SHALL NOT be
used to apply, because it leaves the ledger unchanged and lets the next bring-up silently replay. Rows present
in the ledger with no file on disk are `#2048`'s retroactive-deletion drift, are never visited by
`packages/common/migrate.py`'s loop, and SHALL NOT block applying the pending set. The apply SHALL be wrapped
by a role audit on both sides — audit-only before the superuser write session, and the full
`scripts/node27_provision_write_roles.sh` with a clean strict audit after — and SHALL run inside a window with
the ingest and download timers stopped. Acceptance SHALL be read from the database's own catalogs (applied
ledger rows, `information_schema.columns`, `timescaledb_information.dimensions`), never from the apply
command's exit code alone.

#### Scenario: a national read route is served against a database missing a projected column

- **WHEN** display code that projects `core.river_network_version.geometry_generation` serves
  `hydro-national/{variable}`, `hydro-national/{source}/{cycle}`, `river-network-national` or `/api/v1/layers`
  against a node-27 database whose ledger does not contain the migration that adds that column
- **THEN** the three national tile routes return 500 unconditionally — each computes its digest before fetching
  the tile — and `/api/v1/layers` returns 500 whenever a display-ready run exists, returning 200 with an empty
  layer list otherwise, because `apps/api/routes/hydro_display.py` returns early on `display_ready_run(session) is None`
  and never reaches the digest; and the recorded remedy is to apply the pending migration set with
  `packages.common.migrate` in a timer-stopped window before serving that code, not to restart the service or
  to roll the checkout back

#### Scenario: the pending set is applied and verified from the catalogs

- **WHEN** the pending set is applied on node-27 with `packages.common.migrate` as the `nhms` owner role
- **THEN** every pending filename appears in `public.schema_migrations`, `core.river_network_version.geometry_generation`
  reads `integer` / `is_nullable = NO` / default `0` with no row distinct from 0, the full
  `scripts/node27_provision_write_roles.sh` exits 0 with a clean strict audit, and the same four surfaces
  answer with zero 500 — the three national tile routes returning 200, or the expected 424 when no
  display-ready run exists, and `/api/v1/layers` returning 200 in either case
