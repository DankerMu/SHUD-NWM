## ADDED Requirements

### Requirement: active_flag authority is declared

`docs/spec/03_database_design.md` SHALL state, next to the `core.basin_version` and `core.model_instance` definitions, which reader each `active_flag` is authoritative for: `core.basin_version.active_flag` carries no authority for the compute plane and is not a display-membership flag: the importer writes a hardcoded `false`, no UPDATE path touches it, the backend reads it only as an `ORDER BY` tiebreak, and it is passed through `GET /api/v1/basins/{basin_id}/versions` to the frontend, which uses it only to pick a basin's default selected version (a no-op while every row is `false`; setting any row `true` changes that basin's default selected version); `core.model_instance.active_flag` is the display-membership and lifecycle authority (national river-network MVT membership and frontend active-model counts read it); the compute plane's authority is the node-22 file-registry manifest, which the DB-free scheduler reads instead of either DB flag. `openspec/glossary.md` SHALL define the three meanings as domain terms, and the existing runbook sentences that call the `basin_version` flag meaningless SHALL link to the spec statement.

#### Scenario: DB reader does not conclude nothing is running

- **WHEN** a reader queries `core.basin_version` and sees every `active_flag` false
- **THEN** the spec statement they are pointed to says that column carries no compute authority, enumerates its real readers (backend tiebreak, API passthrough, frontend default-version pick), and names the file-registry manifest as the compute authority

#### Scenario: Display membership is traced to the right flag

- **WHEN** a reader asks why baseline `core.model_instance` rows are active while the `dg_*` rows that run on node-22 are not
- **THEN** the spec statement says display membership reads `core.model_instance.active_flag` (the MVT predicate has no baseline/variant test; as of the 2026-09-02 counts only baseline rows are `true`), that `dg_*` rows are not activated through the DB lifecycle channel, and that the two planes are not synchronized by design
- **AND** no DB row is changed by this change
