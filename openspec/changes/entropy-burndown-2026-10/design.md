# Design: entropy-burndown-2026-10

## Context

Measured facts (audit at `f7be77f22`, methods in `docs/governance/entropy-audit-2026-10-16.md` §1 and §3):

- Upward imports from `packages/common` into workers, by target symbol: `workers.forcing_producer.direct_grid_contract`
  (from `model_registry.py` ×4, `model_registry_contracts.py`, `station_set_flip.py`, `state_clone.py`),
  `workers.data_adapters.base` (`state_manager.py`, `state_cli.py`), `workers.data_adapters.region`
  (`grid_registry_bbox_guard.py`), `workers.mapping_builder.rewrite` (`state_clone.py`, four symbols:
  `verify_hydrologic_core_fingerprint_equal`, `HYDROLOGIC_CORE_FINGERPRINT_LABELS`, `STATE_COMPATIBILITY_SURFACES`,
  `MissingPackageFileError`). Importer counts in non-test code: `direct_grid_contract` 13, `data_adapters.base` 47,
  `region` 3, `rewrite` 7. `data_adapters.base` also hosts the shared `ensure_utc` / `parse_cycle_time` /
  `format_cycle_time` helpers that 44 modules import.
- Status sets: pipeline terminal set in `services/orchestrator/{chain_types.py:15, chain_runtime_utils.py:32,
  file_orchestration_journal.py:621}` (with `reservation_lost`) and `{scheduler.py:354, scheduler_candidates.py:74,
  persistence.py:9}`, `apps/api/routes/pipeline.py:61`, `schemas/pipeline_job.schema.json` (without); success set
  `{succeeded, complete, published}` copied in `scheduler_state_types.py:58, chain.py:216, chain_runtime_utils.py:41,
  chain_source_cycle.py:12, chain_repository_state.py:23, retry.py:80`; hydro run status in the DB enum (10 values),
  `schemas/run_status.schema.json` (14 values incl. `blocked`, `unavailable`, `partial`), and code sets adding
  `complete`, `completed`, `done`, `already_done`; cycle status `apps/api/routes/pipeline.py:80` vs
  `packages/common/met_store.py:16`; Slurm raw states in `slurm_validation.py:78`, `real_backend.py:172`,
  `scripts/run_qhh_continuous.py:70`; QC verdict spellings `passed`/`pass`/`PASS`/`BLOCKED`.
- Lint: `[tool.ruff.lint] select = ["E","F","I"]`; no mypy; 569 broad excepts, 269 `except: pass/continue` sites,
  `Any` in annotations per module 0–57 %.
- Frontend: `src/pages/hydroMet/bootstrap.ts` imported by `stores/hydroMetProductData.ts`, `stores/stationLayerData.ts`,
  `components/map/M11StationForcingPopup.tsx`, `components/map/M11RiverForecastPanel.tsx`; hand-typed
  `PipelineStatus`/`JobStatus` in `lib/constants.ts:10,18` with generated equivalents in `api/types.ts:1818,1835`;
  `failedStatuses` duplicated in `components/monitoring/BasinFailures.tsx:19` and `JobsTable.tsx:39`; one shared
  envelope `api/response.ts` (`ApiErrorShape`, `getApiErrorMessage`, `unwrapApiData`) beside six local normalizers;
  `tsconfig.app.json` is `strict` with unused checks; no ESLint anywhere; `check:api-types` exists as a script but the
  CI job runs only typecheck, check:types, build, test, check:bundle.
- Governance: `scripts/governance/entropy_audit/check_stale_routes.py::_path_is_route_authority_expanded_scope` already
  treats `openspec/changes/archive/` as expanded authority scope (which is why 665 findings land there);
  `tests/test_entropy_audit_baseline_writer_summary.py:76-77` pins `total_test_files == 247` and
  `total_instruction_files == 3` as literals read from the tracked baseline.
- Journal and selector decomposition: cluster map and extraction order are recorded in tasks.md §5 and §6 (from the
  read-only structural mapping run during Stage 2); facade files stay as re-export surfaces until contract.

## Goals / Non-Goals

**Goals:**
- `packages/` has no upward import and CI keeps it that way.
- Every status/verdict set is imported from one module and provably equals the schema and DB enum.
- Each time/normalizer helper concept has one definition.
- New broad excepts and new `Any` cannot land without an explicit ratchet change.
- The frontend has a lint gate, a leaf-only `pages/`, generated status types and one API error path.
- The two largest files are decomposed without any importer noticing.
- Nine critical modules carry scoped instructions; the governance report stops counting archived specs; the baseline can
  be refreshed by the repository writer.

**Non-Goals:**
- Any runtime behaviour change: no status value is added or removed from what the runtime already produces; schemas and
  the API tuple are brought up to runtime truth, not the other way round (user decision).
- Enabling `ANN401` or any type checker (mypy/pyright): open question, see below.
- Clearing the frozen ruff inventory or the existing `Any` count to zero; the ratchet only forbids growth.
- node-22 configuration, units, env files or alerting (change `node22-config-hygiene`).
- The Python 3.11 pin switch (#1831), the NFS mount, the six node-22 stashes.
- Frontend behaviour or layout changes beyond relocation and type/normalizer unification; no new ESLint rules beyond the
  three named.
- Rewriting `openspec/changes/archive` contents or the DOC_STATUS marker semantics.
- Reducing `tests/test_production_scheduler.py` (61 249 lines): test-tree layout is informational in the audit.

## Decisions

### D1 Cycle break by contract extraction into `packages/common`, not a new package (user: pytest guard)
The symbols `packages/common` needs move into `packages/common/contracts/` as three modules: `direct_grid.py` (the
whole of `direct_grid_contract.py`, 519 lines, which imports only `packages.common.source_identity`),
`time_normalization.py` (`ensure_utc`, `parse_cycle_time`, `format_cycle_time` and the other pure helpers from
`data_adapters/base.py`; the adapter base class stays in workers), `region.py` (the 75-line region helpers) and
`hydrologic_fingerprint.py` (the four `rewrite.py` symbols plus the minimal helpers they close over; the 2 298-line
rewrite algorithm stays in `workers/mapping_builder`). Each original worker module becomes a re-export shim
(`from packages.common.contracts.x import *` plus explicit `__all__`) so all 70 importers keep working (expand). After
every importer in the repository is migrated, the shims are deleted (contract). `workers/shud_runtime/runtime.py`'s
import of `services.orchestrator.time_consistency` moves the needed helper into `packages/common/contracts/
time_normalization.py` the same way. Alternative rejected: a new `packages/contracts` distribution, because
`packages/common` is already the shared layer and a second one adds a naming decision with no enforcement benefit.

### D2 Layering guard is a pytest over the AST (user)
`tests/test_import_layering_guard.py` walks every `.py` under `packages/` and fails on any `import`/`from` whose
top-level package is `workers`, `services`, `apps` or `scripts`, and every `.py` under `workers/` importing `services`
or `apps`. It runs in the normal targeted lane (it is selected whenever `packages/**` or `workers/**` changes, via
`scripts/select_ci_tests.py` guarded-owner rules) and in the full lane. An allowlist file
`tests/import_layering_allowlist.json` holds the shims' own re-export lines during the expand window and must be empty
at contract; the test fails if the allowlist names a file that no longer imports upward (dead entries rot).
Alternative rejected: import-linter (new dependency, second config language).

### D3 Status vocabulary module and parity tests (user: code is truth, schemas follow)
`packages/common/status_vocab.py` defines frozensets and `Literal` aliases: `PIPELINE_JOB_TERMINAL`,
`PIPELINE_JOB_SUCCESS`, `PIPELINE_JOB_STATUSES`, `HYDRO_RUN_STATUSES`, `DISPLAY_READY_RUN_STATUSES`,
`CYCLE_STATUSES`, `SLURM_RAW_STATES`, `QC_VERDICTS`. The content of each set is the union of what the runtime currently
produces, derived from the sites listed in Context; where sites disagree, the superset wins and the design records the
delta (e.g. `reservation_lost` joins the terminal set; `complete`/`published` join the pipeline-job success set and the
schema enum; `frequency_done`/`pending` from the DB enum join `HYDRO_RUN_STATUSES`). `tests/test_status_vocab_parity.py`
asserts: `PIPELINE_JOB_STATUSES == schemas/pipeline_job.schema.json status enum`, `HYDRO_RUN_STATUSES ⊇ DB enum` and
`== schemas/run_status.schema.json enum`, `CYCLE_STATUSES == met.cycle_status enum`, and that no file under
`services/`, `apps/`, `packages/`, `scripts/`, `workers/` defines a frozenset/tuple literal containing both
`"succeeded"` and `"failed"` outside `status_vocab.py` (grep-style AST scan with an allowlist that must shrink). QC
verdict spelling is unified to lowercase `passed`/`failed`/`blocked` at the vocabulary level with a documented mapping
function for receipts that still carry uppercase; receipt schemas keep their current spelling (no runtime change).

### D4 Helper consolidation lands after D1 (user)
Targets: utc-now → `packages/common/contracts/time_normalization.py::utc_now`; ISO parse → `parse_iso_utc`; format →
`format_iso_utc`; source normalizer → existing `packages/common/source_identity.py::normalize_source_id`. One batch per
top-level package (`packages`, `services`, `workers`, `apps`, `scripts`); each batch replaces local definitions with
imports and deletes the local definition; a count test `tests/test_time_helper_consolidation.py` asserts the number of
top-level functions named like `_?utc_?now`, `_?parse_(iso|utc|time)`, `_?format_time`, `_?normalize_source` outside the
canonical module does not exceed a ceiling file that each batch lowers.

### D5 Lint ratchet: ruff rules with frozen inventory plus `Any` count ceiling (user)
`pyproject.toml` `[tool.ruff.lint] select` gains `"B"` and `"BLE"`; `[tool.ruff.lint.per-file-ignores]` lists every file
that currently violates, generated by `ruff check --select B,BLE --output-format json` at the time of the change and
committed; CI already runs `uv run ruff check .`, so new violations fail. `tests/test_any_ratchet.py` counts `Any`
occurrences in annotations (AST, same method as the audit scanner) over `apps/ packages/ services/ workers/ scripts/`,
compares to `tests/any_ratchet_ceiling.json` (`{"ceiling": <n>}`), fails above the ceiling, and fails when the count is
more than 5 % below the ceiling (forcing the ceiling to be lowered in the same PR so the ratchet does not slacken).
`ANN401` stays off (open question).

### D6 Frontend: three-rule ESLint, leaf-only pages, generated statuses, one normalizer (user)
`eslint.config.js` (flat) with `@typescript-eslint/no-explicit-any`, `import/no-cycle`, and `no-restricted-imports`
forbidding `@/pages/*` and relative `pages/` imports from `src/stores`, `src/components`, `src/lib`, `src/api`,
`src/hooks`. `package.json` gains `"lint": "eslint src"`; `.github/workflows/ci.yml` `frontend-build` runs `pnpm lint`
and `pnpm check:api-types` after typecheck. `src/pages/hydroMet/bootstrap.ts` moves to
`src/stores/hydroMetBootstrap.ts` (same exports), so the cycle rule passes without exceptions. `lib/constants.ts`
`PipelineStatus`/`JobStatus` become aliases of the generated `components["schemas"]` types; `failedStatuses` becomes one
exported constant in `lib/constants.ts`. API failure handling: every fetch site in `src/` resolves errors through
`api/response.ts::getApiErrorMessage` (the six local normalizers become thin wrappers or are deleted); the two
silent-null sites (`pages/m11/useNationalBasinGeo.ts`, `lib/mvtLayerMetadata.ts`) surface the normalized error to their
store instead of returning `null`/`[]` silently, with no change to what the UI renders when data is absent. Alternative
rejected: Biome (another toolchain for three rules).

### D7 Decomposition by responsibility cluster, facade-preserving (user)
For each of `file_orchestration_journal.py` and `select_ci_tests.py`: one task per cluster moves that cluster's
definitions to a sibling module (`services/orchestrator/journal_<cluster>.py`, `scripts/select_ci_tests_<cluster>.py` or
a `scripts/ci_selector/` package), the original file re-exports every moved public name so all importers and all tests
keep passing unchanged; the final task deletes the re-exports once `grep` shows no importer of the facade name and the
facade shrinks to the orchestration code that genuinely belongs to it. Behaviour oracle per step: the existing test
files for the two modules pass unchanged, and for the selector additionally `uv run python scripts/select_ci_tests.py`
over a fixed list of changed paths (recorded in the task) yields byte-identical output before and after. Cluster
boundaries and extraction order are in tasks.md §5/§6; the highest-risk step is flagged there.

### D8 Governance noise and baseline (user)
`check_stale_routes.py::_check_stale_route_tokens` skips paths under `openspec/changes/archive/` (one path-family
predicate local to that check; `_path_is_route_authority_expanded_scope` is not changed for `docs/archived/` or
`docs/runbooks/`). `tests/test_entropy_audit_baseline_writer_summary.py` derives the expected test and instruction
counts from the tracked baseline file's `summary` instead of literals, keeping the semantic assertions
(`v1_summary_* == summary`). Then `uv run python scripts/governance/write_entropy_baseline.py` is run once and the
refreshed `latest.json` plus the archived previous snapshot are committed.

### D9 Scoped AGENTS.md (user: all nine in one issue)
`packages/common`, `packages/scheduler`, `workers/model_registry`, `workers/data_adapters`,
`workers/forcing_producer`, `workers/mapping_builder`, `workers/grid_registry`, `services/slurm_gateway`, `scripts`
(nine; `services/precip` is a 7-file module and is included in place of `packages/scheduler` if the implementer finds
`packages/scheduler` below the 5-file threshold at implementation time; the count stays nine) each get an AGENTS.md with
the four sections the existing scoped files share (`Required Reading`, an ownership/boundary section, a fences or
error-model section, `Focused Verification`), each verification command runnable locally. The governance docs gate is
the oracle. One issue, `Width exception: merged-tasks`, because the files are independent but share one author pass,
one verification path and no code.

### D10 Legacy store reference retirement (fact-check: table dropped 2026-09-19)
`packages/common/node27_pgdata_workload_plan.py` drops `LEGACY_HYPERTABLE` from the SQL `IN` list and the default
`expected_relations`; `node27_timeseries_discovery.py` keeps its catalog-driven derivation (it only applies when present);
runbook/spec text listed in the audit gets the DOC_STATUS section marker or the sentence rewritten to past tense.

## Sketch seams under test

- `tests/test_import_layering_guard.py` over the real tree (D1/D2): the highest seam; the whole structural goal is its
  pass/fail.
- `packages/common/status_vocab.py` public sets vs schema/DB files (D3): one parity test is the oracle for every
  migrated site.
- `uv run ruff check .` and `tests/test_any_ratchet.py` (D5): the gate itself is the seam.
- `cd apps/frontend && pnpm lint && pnpm typecheck && pnpm test` (D6): existing vitest suites unchanged plus the lint
  exit code.
- Existing test files of the two decomposed modules plus the selector's byte-identical output check (D7): behaviour
  preservation at the only public boundary each has.
- `uv run python scripts/governance/audit_repo_entropy.py --format json` finding counts and
  `tests/test_entropy_audit_baseline_writer_summary.py` (D8/D9/D10).

## Risks / Trade-offs

- The expand window of D1 leaves two import paths for the same symbol; the allowlist-with-rot check in D2 bounds it.
- D3 adds `complete`/`published` to the pipeline-job schema: consumers validating against the schema accept more values
  than before; this is the runtime truth today, so nothing that worked can break, but a downstream that relied on the
  schema to reject them would be loosened. No such consumer was found (`grep` of `pipeline_job.schema.json` users).
- D5 freezes a large `per-file-ignores` list; it is generated, not hand-written, and shrinks only by cleanup PRs.
- D6 `import/no-cycle` on a 115-file tree is the slowest rule; measured impact belongs in the issue's evidence.
- D7 touches a 17.5 k-line file that receives ~1.7 commits/day; each extraction PR must rebase quickly or conflict.
  Mitigation: small clusters, one PR at a time, facade keeps names stable.

## Migration Plan

Expand–contract for D1 (shims) and D7 (facades). Everything else is additive or a pure move with tests. No data
migration, no deployment step; node-27 display receipts are not required because no display runtime behaviour changes
(the frontend changes are structural and are covered by the mocked-regression lane plus typecheck).

## Open Questions

- Q1: when to enable `ANN401` (after the `Any` ratchet has reached a threshold, or never, relying on the ratchet).
- Q2: whether `scripts/` should be split into a package (`scripts/ci_selector/`) or stay flat files for the selector
  siblings; tasks.md picks the flat layout and the implementer may propose the package if the sibling count exceeds four.

## Not yet specified

- A type checker roll-out (mypy/pyright): the problem (which modules first, strictness) is not stated precisely enough.
- Decomposition of the next tier of large files (`packages/common/state_manager.py` 5 101, `forecast_store.py` 5 036,
  `services/production_closure/two_node_e2e_evidence.py` 4 512): same method as D7 but their cluster maps do not exist yet.
- Naming-variant reduction for the 48 domain spellings (run_id/job_id/pipeline_job_id, cycle/cycle_time/issue_time):
  needs a glossary pass (`grill-with-docs`) before it can be cut.

## Grill ledger (2026-10-16)

- [decided:user] two changes; this one is code, oracle local pytest + node-27 where display runtime changes (none here).
- [decided:user] journal + selector decomposition included (D7), cluster extraction with facade.
- [decided:user] helper dedupe included, batched per top-level package (D4).
- [decided:user] baseline: docs PR leaves latest.json; separate issue refreshes via writer and unpins the test (D8).
- [decided:user] cycle enforcement via pytest AST guard (D2); contract subset extraction + shims (D1).
- [decided:user] status vocabulary: code is truth, schemas follow (D3).
- [decided:user] lint ratchet: ruff B+BLE frozen inventory + Any count ratchet (D5).
- [decided:user] ESLint with three rules + check:api-types in CI (D6).
- [decided:user] stale-display-route-token skips openspec/changes/archive (D8).
- [decided:user] nine AGENTS.md in one issue with merged-tasks exception (D9).
- [decided:fact-check] `reservation_lost` is runtime-real (16 producing/consuming files); `hydro.river_timeseries_legacy` was dropped 2026-09-19 (glossary).
- [open] Q1 ANN401 timing.
