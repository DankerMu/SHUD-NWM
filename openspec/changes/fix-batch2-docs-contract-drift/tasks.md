Fixture level: compact (docs/spec text + one static meta-test; no runtime behavior). Upstream suggested level: absent.

Change surface:
- Docs and promoted specs listed in proposal.md; `tests/test_select_ci_tests.py` recipe meta-test.
Must preserve:
- Every command a doc keeps remains copy-paste runnable; Python file lists in ruff commands byte-unchanged (#1935).
- `basins-registry-import` requirement `Output-river geometry backfill writes only the target network version` and `mvt-tile-contract` unchanged; still-dead legacy tokens stay in the SHALL-remove grep scenario.
- `tests/test_select_ci_tests.py` existing assertions keep passing.
Seams under test:
- Recipe meta-test (`test_registry_partition_live_commands_name_all_seven_suites`) for #2059; `openspec validate --strict` for spec deltas; collect/grep probes below for the rest.
Risk packs:
- Public API / CLI / script entry: not selected — no entrypoint change (doc recipes only).
- Config / project setup: not selected — no config/CI change (#1935 deliberately avoids `ci.yml`).
- File IO / path safety / overwrite: not selected — none.
- Schema / columns / units / field names: selected — `hydro.run_status` enum text must equal migration ledger (000003 + 000013).
- Auth / permissions / secrets: not selected — none.
- Concurrency / shared state / ordering: not selected — none.
- Resource limits / discovery: not selected — none.
- Legacy compatibility / examples: selected — archived change names / retired nodeids in recipes.
- Error handling / rollback / partial outputs: not selected — none.
- Release / packaging: not selected — none.
- Documentation / migration notes: selected — all eight issues.
- Domain (PostGIS/Timescale, SHUD, forcing, Slurm, providers): not selected — text only; #2155 describes existing backfill behavior without changing it.

## Deviations (recorded up front)

- Fixture review: iteration 1 revise, iteration 2 revise (check precision only, no missing axes); the three precision fixes applied directly without a third rerun (bounded at two).

- #2055 and #2234 path A say "tests diff empty"; this batch edits `tests/test_select_ci_tests.py` for #2059 only (static recipe meta-test). No selector/importer behavior test changes.
- #2234 probe numbers drifted since filing: selector-source diff now selects 3 targets (incl. `tests/test_river_segment_write_surface_scan.py`, #2185). Spec text is corrected to numbers re-measured at HEAD, recorded in the implementer report.
- #1862 acceptance `openspec validate reconcile-identity-fallback-reason` is itself a dead archived-change recipe; replaced by `openspec validate --all --strict --no-interactive`.
- #2047: migration files are not the live enum — node-27 `hydro.run_status` has 12 members incl. `frequency_done` (b97c16e2 rewrote 000003 after apply; `docs/review-loop-log.jsonl` #2037 line). Docs list the ledger values AND note `frequency_done` is retired but still present in the live DB enum.

## 1. Implementation

- [x] 1.1 #2059 `docs/VALIDATION.md` real-Basins smoke fence: `NHMS_RUN_REAL_BASINS_IMPORT=1` stays first (meta-test slices text after it, `tests/test_select_ci_tests.py` ~:19018), then `NHMS_RUN_INTEGRATION=1`, `NHMS_INTEGRATION_DATABASE_URL=...`; no bare `DATABASE_URL`; meta-test asserts both gate names and `re.search(r"(?<![A-Z_])DATABASE_URL=", smoke) is None` (plain substring would match `NHMS_INTEGRATION_DATABASE_URL=`).
- [x] 1.2 #2055 runbook nodeid retarget; spec delta for `River segment row classes…` (drafted).
- [x] 1.3 #2047 run_status carriers (`docs/spec/03_database_design.md`, `docs/appendices/C_database_schema_draft.md`, `docs/spec/01_architecture_and_flow.md`, `docs/runbooks/forcing-copyback-backfill.md`, `docs/modules/14_tile_publication_service_{spec,design}.md`) = ledger values in migration order (`pending` between `staged` and `submitted`), plus retired-but-live `frequency_done` note; architecture doc stage count = `STAGE_NAMES` length; `met.cycle_status` `complete` placed explicitly as a different enum; runbook SQL valid on fresh DB.
- [x] 1.4 #2155 spec delta: `River segments are imported…` and `Deprecated cross-gap fallback paths…` corrected — four output-river tokens removed from SHALL-remove list and grep scenario; `Output-river backfill is not re-introduced` scenario replaced with pointer to `Output-river geometry backfill writes only the target network version`; stale `gis/seg.shp` reason fixed; wording consistent with `openspec/glossary.md` and `mvt-tile-contract`.
- [x] 1.5 #1936 `openspec validate m[0-9]…` recipes → `openspec validate --all --strict --no-interactive` or `--type spec <capability>` for the 6 M10 capabilities; historical receipt lines kept only outside bash fences with provenance notes; `qhh-mvp-*` edits limited to recipe lines.
- [x] 1.6 #1935 strip `.md` args from 5 ruff commands (1 `docs/VALIDATION.md`, 4 `docs/validation/production-closure.md`); Python path lists identical; docs targets routed to markdownlint (CI form); `progress.md` annotated as outside markdownlint CI scope.
- [x] 1.7 #1862 `failed-basin-retry.md` "unique" sentence = D3; outcome table and `ambiguous_fallback_match` text unchanged.
- [x] 1.8 #2234 `ci-contract-baseline` delta for the two requirements containing `A contract-only diff selects the dependent closure` and `selector-development PRs fire the flag honestly`: text matches re-measured behavior; `meta_guard_only` statements for `["scripts/select_ci_tests.py"]` consistent across the requirement.

## 2. Verification (input → expected)

- [x] 2.1 #2059 `uv run pytest -q tests/test_select_ci_tests.py` (full file) → pass. RED: deleting `NHMS_RUN_INTEGRATION=1`, and separately `NHMS_INTEGRATION_DATABASE_URL`, from the fence makes the meta-test fail each time; re-adding `export DATABASE_URL=...` (pre-fix form) also fails it. Probe (no DB): with only `NHMS_RUN_REAL_BASINS_IMPORT=1 DATABASE_URL=x`, `tests/test_basins_registry_import_db.py::test_real_basins_import_smoke_is_gated` is skipped for the integration reason; with the three canonical vars `tests/conftest.py::_integration_skip_reason()` returns None.
- [x] 2.2 #2234 probe at HEAD (orchestrator-measured 2026-09-14, `select_tests` + `_collection_smoke_required`): `["scripts/select_ci_tests.py"]` → count 3 (`tests/test_river_segment_write_surface_scan.py`, `tests/test_select_ci_tests.py`, `tests/test_timescale_write_guard_wire_site_invariant.py`), `meta_guard_only=false`, `collection_smoke_required=true`; `["tests/test_select_ci_tests.py"]` → count 1, `meta_guard_only=true`, `collection_smoke_required=true`; `["packages/common/node27_container_contract.py"]` → count 16, core-smoke overlap 5 of 5, `collection_smoke_required=false`. Re-run yields the same values and the spec delta states exactly these facts; full `tests/test_select_ci_tests.py` pass (2.1).
- [x] 2.3 #2055 collect-only: new nodeid → 1 collected; old `tests/test_basins_registry_import.py::test_pr2_contract_reach_rows_single_part_and_crosswalk_count` → rc 4; issue `rg` sweep (with its exclusions) → 0; `git diff --stat` empty for `openspec/changes/archive/**`, `docs/bugs.md`, `tests/fixtures/basins_registry_partition_oracle.json`.
- [x] 2.4 #2047 `git grep -n frequency_done -- docs/spec docs/appendices docs/runbooks/forcing-copyback-backfill.md` → only the retired-but-live note lines (listed); enum lists equal migration order; stage count = `STAGE_NAMES` length; `git diff --stat` empty for `docs/runbooks/qhh-*.md` (except #1936 recipe lines), `docs/plans/**`, `docs/bugs.md`.
- [x] 2.5 #2155 `grep -n "_backfill_output_segment_geometry\|_ensure_output_river_segments\|_output_river_segment_rows" openspec/changes/fix-batch2-docs-contract-drift/specs/basins-registry-import/spec.md` → no SHALL-remove context; remaining SHALL-remove tokens grep over repo (excluding openspec archive/changes) → 0 hits, output pasted; kept requirement already states reach-row source, geom/length_m/Type copy, `only_missing`, `geometry_generation` bump.
- [x] 2.6 #1936 `git grep -nE "openspec validate m[0-9]" -- README.md progress.md docs` → only listed historical receipt lines outside bash fences; `openspec validate --all --strict --no-interactive` → all pass; each of 6 M10 capability specs validates with `--type spec`.
- [x] 2.7 #1935 `for f in docs/VALIDATION.md docs/validation/production-closure.md; do awk '/\\$/{sub(/\\$/,"");printf "%s",$0;next}1' "$f" | grep -nE 'ruff (check|format)[^|&;]*\.md'; done` → no output; Python path lists before/after identical (pasted); each edited ruff command exits 0 locally.
- [x] 2.8 #1862 `grep -n "more than one row from this" docs/runbooks/failed-basin-retry.md` → 0; outcome table diff empty.
- [x] 2.9 `openspec validate fix-batch2-docs-contract-drift --strict --no-interactive` → valid, with deltas for both `basins-registry-import` (3 requirements) and `ci-contract-baseline`.
- [x] 2.10 `uv run ruff check .` → clean; markdownlint in CI form over `docs/**/*.md` (repo config, same tool/version as `.github/workflows/ci.yml` Markdown Lint job) → exit 0, which covers #1935 targets `docs/runbooks/api-latency.md` and `docs/runbooks/tile-publish-error.md`.

## Non-goals

- Deleting the output-river backfill family; changing selector behavior; editing `ci.yml`; runtime code; migrating the live DB enum.
