# retire-dead-branches-for-retired-surfaces (#3149)

## Why

The round-3 entropy audit (2026-11-16) flagged code branches and defaults tied to surfaces that have since been
retired:
- the river-segments list route (#2866);
- the station tile route (#2773 item 4);
- `workers/sbatch_templates/` (#363);
- the 14T `/data/GHDC` PGDATA target (#3059).

Each one has to be proven unreachable before it is deleted, or kept with a recorded reason.

## Verdicts (orchestrator survey + fixture review, re-verified by the implementer)

1. **OpenAPI restored schemas: KEEP.** `apps/api/openapi_patching.py::_patch_river_segment_openapi` restores the
   response body of the live detail route, `GET /api/v1/basin-versions/{basin_version_id}/river-segments/{segment_id}`.

   `GeoJsonLineString` is published, and the frontend consumes it directly:
   - `apps/frontend/src/lib/m11/overviewDataGeometryBudget.ts`;
   - `apps/frontend/src/components/map/m11MapBuilders.ts`;
   - `apps/frontend/src/lib/m11/overviewDataContractTypes.ts`.

   Removing it would change the published contract and break `tsc`. The list route itself is already gone.
2. **Station tile route remnants: DELETE the display-rule carry. KEEP the `station_set_flip` module and its own rule.**
   - The `services/tiles/mvt.py` rule and the `apps/api/routes/hydro_display*.py` rule carry the
     `DIRECT_GRID_DISPLAY_CUTOVER_FLIP_TESTS` partitions by hand: `scripts/select_ci_tests_rules.py` ~:2451-2454 and
     ~:2600-2603. The only reason for that carry was that `_mvt_set` modelled the station MVT layer. That layer went
     with the retired route.
   - Neither partition imports a display module any more; `tests/test_select_ci_tests.py` asserts this.
   - No code in `apps/api` or `services/tiles` reads `met.met_station.active_flag`.
   - Living spec `ci-test-selector-routing` requires only the `station_set_flip.py` rule, so the carry is removed from
     both display rules. The comments and pins that describe it are updated to match.
   - Living spec `ci-contract-baseline` ("Guarded-module selector rules MUST cover their non-gated importer
     closure") still lists both flip partitions in the `services/tiles/mvt.py` THEN. Its stated reason is that
     `_atomic` arrives through a one-hop import via `hydro_display`, which has been false since #2773. A MODIFIED delta
     rewrites that THEN: the partitions are carried by the `station_set_flip.py` rule.
   - The module itself and its own selector rule are governed by the living specs `station-set-atomic-flip` and
     `ci-test-selector-routing`. That the flip hook has no production caller is already reported in
     `docs/governance/API_CONTRACT_RETIREMENT_INVENTORY.md` and is not decided here.
   - **Conditional: `"met-stations"` in `apps/api/routes/hydro_display_catalog.py` (~:151).** Delete it only if
     `source_version` for the met-stations layer is proven never to reach any response or SQL. If that cannot be
     proven, KEEP it with the reason recorded.
3. **Legacy sbatch path: KEEP.**
   - There is no fallback code: `template_dir` defaults to `infra/sbatch`, and the `workers/sbatch_templates` directory
     does not exist.
   - The docstring mention is a retirement note that the entropy audit allowlists on purpose.
   - `RETIRED_ACTIVE_TREE_PREFIXES` is a fail-closed guard.
4. **pgdata target default: KEEP the default, FIX the comments.**
   - `packages/common/node27_pgdata_migrate.py` `DEFAULTS` holds the frozen #2240 identity. It has been inert since
     #3059: production runs refuse with `PRODUCTION_RELOCATION_RETIRED`, and
     `tests/test_node27_pgdata_relocation_retired.py` pins that.
   - `scripts/node27_resource_governance.py`'s `/home/nwm/nhms-pgdata` default is kept on purpose, as
     `infra/env/node27-resource-governance.example` explains. It is recorded as a keep and not changed.
   - Two comments in `packages/common/node27_container_contract.py` still describe the 2026-08-06 layout: the mount
     comment (~:250-262) and the docstring (~:311-318). They are updated to the live `docker inspect nhms-db` and
     `pg_tablespace` output measured on 2026-11-16:
     - PGDATA is `/home/nwm/nhms-primary/pgdata` -> `/home/postgres/pgdata/data`;
     - the tablespace bind is a stub, `/home/nwm/nhms-tablespace-ghdc-stub` -> `/home/postgres/pgdata/tablespaces/ghdc`;
     - `pg_tablespace` lists only `pg_default` and `pg_global`;
     - evidence is unchanged, `/home/nwm/nhms-evidence` -> `/var/lib/postgresql/evidence`.

     The conclusion still holds, because only `evidence` lands under the prefix.
5. **Found during the survey: DELETE the dead archived-change allowlist.**
   - `scripts/governance/entropy_audit/check_paths_and_api.py` `_placeholder_path_allowlist_reason` has branches for
     `openspec/changes/governance-2-legacy-dead-code-retirement/` and
     `openspec/changes/governance-5-e1-entropy-baseline-burndown/`. Both changes were archived on 2026-06-18, and the
     scanner only ever sees `openspec/changes/archive/...`, so neither prefix can match any more.
   - With them go:
     - the reason-to-key mapping that now has no producer (`scripts/governance/entropy_audit/findings.py` ~:110-111),
       along with the already producer-less sibling mapping at ~:108-109;
     - the two whole test functions that exist only for these branches (`tests/test_entropy_audit_retired_paths.py`
       ~:103-180);
     - the sample reason in `docs/governance/entropy-report.example.md`.

## Non-goals

- Wiring or retiring `station_set_flip`.
- Retiring the pgdata migration tool.
- Changing the published OpenAPI.

## Triage

Issue type: tech-debt cleanup.
Fixture level: compact. The change removes selector carry, comments and unreachable audit-allowlist branches. It
makes no production code-path or schema change. The one possible API edit (item 2, conditional) is a branch proven
dead or kept.
Upstream suggested level: absent.
Blast radius:
- Removing the selector carry stops the flip suites from running on display-only diffs. This is intended, since no
  display code touches the flip any more.
- A wrongly deleted allowlist branch would turn an allowlisted placeholder finding into a budget-counted one. That
  changes `metadata.budget_counted_count`, but it is not a hard-gate failure (`placeholder-path-token` is not a
  hard-gate check).
Selected risk packs: Legacy compatibility (verdicts 1-4); Documentation (item 4 comments, item 5 docs).
Evidence floor:
- `uv run ruff check .`;
- the entropy report, before and after: `budget_counted_count` and the allowlisted/unallowlisted split in
  `summary_counts` are unchanged, and the hard gate passes;
- the selector-chosen pytest set, plus `tests/test_select_ci_tests.py`, `tests/test_entropy_audit*.py`,
  `tests/test_openapi*.py`, `tests/test_node27_external_contract_snapshot.py`,
  `tests/test_node27_pgdata_relocation_retired.py`, and the `tests/test_hydro_display*` / `tests/test_layers*` suites
  if the conditional API edit lands, locally and in a node-27 temp worktree;
- `git diff -- openapi/ apps/frontend/` is empty.

The issue's node-27 display `/health` check applies only if the conditional API edit lands; otherwise it is not
applicable, because no API code changes.
