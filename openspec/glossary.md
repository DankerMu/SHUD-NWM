# OpenSpec Glossary

This glossary is the canonical vocabulary for this project: both the
entropy-governance terms used by OpenSpec changes, GitHub issues, scoped
`AGENTS.md` files, and governance reports, and the domain ubiquitous language
used by specs, runbooks, code comments, and issue text. When a scoped
instruction, a runbook, or a code comment needs one of these concepts, link here
or reuse the term exactly instead of introducing a local synonym.

## Governance terms

| Term | Definition |
|---|---|
| active entrypoint | The current public route, CLI, module function, or operational command that callers should use. Historical names may mention it, but only the active entrypoint owns current behavior and verification. |
| legacy redirect alias | A compatibility surface that accepts an old route, name, or command and forwards to an active entrypoint. It is retained for caller continuity and must not be treated as a second product surface or owner. |
| retired active-tree path | A previously active tracked path that must not return to the live source tree unless a new issue explicitly reactivates it. Text mentions can remain as historical evidence when they are marked or allowlisted by governance rules. |
| compatibility facade | A module that keeps old import, monkeypatch, or call surfaces stable while delegating behavior to real owner modules. New facade surface requires inventory or guard evidence; local bug fixes that do not add ownership surface are not facade growth. |
| lane | A bounded validation or evidence responsibility inside a larger workflow, with an owner module, input contract, output/result shape, blocker or finding namespace, focused verification command, and retention condition. |
| budget-counted finding | An unallowlisted active entropy finding that consumes the current cleanup budget. It should map to a cleanup owner, a follow-up issue, or a deliberate accepted disposition. |
| gate-eligible finding | A budget-counted finding whose check ID is in the prepared hard-gate set and whose individual policy marks it eligible for explicit hard-gate failure. Gate eligibility is narrower than budget counting and does not enable CI failure by itself. |
| current authority | The source a reader or agent must consult before treating preserved text as actionable. It can be an active spec, runbook, inventory, source module, test, or documented decision that owns the present contract. |
| historical evidence | Preserved docs, archived specs, examples, logs, or work records kept for auditability, migration context, or regression proof. Historical evidence may explain why old terms exist, but it does not override current authority. |

## Domain terms

Hydrological / registry ubiquitous language. Symbol names are authoritative when
the cited line numbers drift.

| Term | Definition |
|---|---|
| SHUD input reach row | One of the two row classes `core.river_segment` stores under a single `river_network_version_id`. Id shape `<model_id>_reach_<iRiv:06d>`, sourced from the model package's `gis/river.shp`, `properties_json->>'shud_output_river'` absent or `'false'`. It carries the hydraulic parameters and the flow-ordered single-part geometry, and it is the only row class the importer points `core.river_segment_crosswalk` at (`workers/model_registry/basins_registry_import.py::_build_river_segment_crosswalk_rows`). |
| SHUD output river row | The other `core.river_segment` row class under the same `river_network_version_id`. Id shape `<model_id>_shud_riv_<N:06d>`, sourced from the model package's `.sp.riv`, `properties_json->>'shud_output_river' = 'true'`. It carries the SHUD output-series identity that `hydro.river_timeseries` is keyed on; its geometry is backfilled from the matching reach row (`workers/model_registry/basins_registry_import.py::_ensure_output_river_segments`). |
| `segment_count` | The `core.river_network_version.segment_count` column. It counts **reach rows only** — post-PR-2 (#561) it is the `gis/river.shp` record count (`workers/model_registry/basins_geometry.py::parse_basins_geometry`). Import validates that the `river.shp` record count equals the `.sp.riv` reach count, so the two row classes are always equal in number and an unfiltered `select count(*) from core.river_segment where river_network_version_id = …` returns `2 × segment_count` **by design**. Compare against `segment_count` only after filtering on `COALESCE(properties_json->>'shud_output_river','false')`. See `docs/runbooks/current-production-ops.md` §9.1, and #1122 / #1123 for the two investigations that misread the doubled count as duplicate seed rows. |
| `output_segment_count` | The `.sp.riv` reach count. It exists only in the import receipt and in `core.model_instance.resource_profile`; there is **no** `output_segment_count` column on `core.river_network_version`. Equal to `segment_count` for a valid package, by the same import-time validation. |
| `active_flag` (file-registry manifest) | The compute plane's authority. Written per model into the node-22 scheduler registry manifest (`manifest-last.json`) by `scripts/publish_scheduler_file_registry.py` alongside `lifecycle_state`. The DB-free scheduler (`NHMS_SCHEDULER_REGISTRY_BACKEND=file`) reads this and never reads either DB `active_flag`. |
| `active_flag` (`core.model_instance`) | The display and lifecycle authority. Read for national river-network MVT membership (`services/tiles/mvt.py:367`, also `:442`, `:653`, `:691`, `:1411`), for the frontend `activeModelCount` (`apps/frontend/src/lib/m11/overviewDataContracts.ts:408`, `:617`), and by the model-lifecycle API (`packages/common/model_registry.py`). It carries no authority for compute; it is not synchronized with the file-registry manifest. |
| `active_flag` (`core.basin_version`) | Carries no authority for compute or display. The importer writes a hardcoded `false` on every row it creates (`workers/model_registry/basins_registry_import.py:542-548`); no `UPDATE` path anywhere touches the column — the importer's own later `UPDATE core.basin_version` (`basins_registry_import.py:799`) sets only `source_uri`/`checksum`. The two internal write APIs (`POST /api/v1/basins` → `::create_basin_with_version` and `POST /api/v1/basins/{basin_id}/versions` → `::create_basin_version`, both landing in `packages/common/model_registry.py::_insert_basin_version`) do accept `active_flag` in the creation payload and could set it `true` on a new row, but no production ingest uses that path — every production row came from the Basins importer (node-27 read-only count 2026-09-02: 0/44 true). The sole non-test reader is an `ORDER BY active_flag DESC, …` tiebreak in `packages/common/model_registry.py:874`, which is a no-op while every row is `false`. Do not use it to retire a basin version — use `valid_to`. |
| `active_flag` (`met.met_station`) | A fourth, unrelated flag: station selection scoped by `basin_version_id`, read at `packages/common/forecast_store.py:1060` and flipped by `packages/common/station_set_flip.py`. Listed here so a repo-wide grep for `active_flag` finds every meaning accounted for. |

## Usage Rules

- Prefer these exact terms in scoped `AGENTS.md` files and governance specs.
- Do not make a local synonym for a term in this file unless a new OpenSpec
  change updates this glossary first.
- Use `current authority` whenever archived or superseded text could otherwise
  look like live instructions.
- Keep `budget-counted finding` and `gate-eligible finding` separate in reports,
  PR evidence, and issue acceptance criteria.
