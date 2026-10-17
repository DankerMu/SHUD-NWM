# Design: entropy-burndown-2026-10

## Context

Measured facts (audit at `f7be77f22`, methods in `docs/governance/entropy-audit-2026-10-16.md` §1 and §3):

- Upward imports from `packages/common` into workers, by target symbol: `workers.forcing_producer.direct_grid_contract`
  (from `model_registry.py` ×4, `model_registry_contracts.py`, `station_set_flip.py`, `state_clone.py`),
  `workers.data_adapters.base` (`state_manager.py`, `state_cli.py`), `workers.data_adapters.region`
  (`grid_registry_bbox_guard.py`), `workers.mapping_builder.rewrite` (`state_clone.py`, five symbols:
  `verify_hydrologic_core_fingerprint_equal`, `HYDROLOGIC_CORE_FINGERPRINT_LABELS`, `STATE_COMPATIBILITY_SURFACES`,
  `MissingPackageFileError`, `HydrologicCoreFingerprintMismatchError`; both exceptions subclass `SpAttRewriteError`).
  Importer counts in non-test code (by `git grep`, deduplicated per file): `direct_grid_contract` 12, `data_adapters.base`
  44, `region` 2, `rewrite` 7; `services/orchestrator/chain.py` imports `data_adapters.base` as a module object and
  `chain_compat_runtime.py` reads cycle aliases from it with `getattr`, which `tests/test_orchestration_chain.py` pins by
  identity. `data_adapters.base` also hosts the shared `ensure_utc` / `parse_cycle_time` /
  `format_cycle_time` helpers that 44 modules import.
- Status sets (every site below is a distinct named set in D3; contents differ on purpose, not by accident):
  pipeline terminal 7-value set with `reservation_lost` in `services/orchestrator/{chain_types.py:15,
  chain_runtime_utils.py:32, file_orchestration_journal.py:621}` (`chain_types` is a deliberately mutable `set` pinned
  by `tests/test_forced_resubmit_veto.py`); 6-value set without it in `scheduler.py:354`, `scheduler_candidates.py:74`;
  `persistence.py:9` 4-value `TERMINAL_STATUS_GUARD` + 2-value overrides; public API tuple `apps/api/routes/pipeline.py:61`
  (11 values) = `schemas/pipeline_job.schema.json` enum, both lacking the runtime-written `reservation_lost`;
  orchestrator-internal transients `reserved`, `cancellation_pending`, `reconcile_unverified`
  (`accepted_submit_identity.py:189`, `chain_stage_execution.py`); read-side success tolerance `{succeeded, complete,
  published}` copied in `scheduler_state_types.py:58, chain.py:216, chain_runtime_utils.py:41, chain_source_cycle.py:12,
  chain_repository_state.py:23, retry.py:80` and inline in `file_orchestration_journal.py`, `persistence.py`,
  `forcing_submit_identity.py` (no pipeline-job writer emits `complete`/`published`; `tile_publisher` and
  `pipeline_job_provenance` write `published` on other records). Hydro: `scheduler_state_types.py:48-61` already hosts
  `DURABLE_HYDRO_SUCCESS_STATUSES` (set, with the code-only `complete`), `HYDRO_RUN_CODE_CLEARING_STATUSES` (frozenset),
  `FAILED_PIPELINE_STATUSES`, pinned by `tests/test_hydro_status_set_parity.py` (#1581, folds every migration and pins
  `frequency_done` as in no set); `schemas/run_status.schema.json` 14 values; display-ready `{succeeded, parsed,
  published}` in `forecast_store.py:28` (tuple) and `hydro_display_constants.py:31` (set); parse-ready `{succeeded,
  parsed, failed}` in `output_parser/parser.py:48`; journal `:707` `{complete, succeeded, parsed, published}`; API route
  `_TERMINAL_HYDRO_STATUSES`. Cycle: `met.cycle_status` enum grows by `ALTER TYPE` in `000013` (`cancelled`) and
  `000027` (`canonical_incomplete`); UI terminal 10-value set `pipeline.py:80`; upsert terminal `{published, cancelled}`
  `met_store.py:16` (failed_* stay overwritable on purpose). Slurm raw states in `slurm_validation.py:78`,
  `real_backend.py:172` (dict keys), `scripts/run_qhh_continuous.py:70`. QC verdict spellings `passed`/`pass`/`PASS`/
  `BLOCKED`; `services/m24_live/receipt.py:57` validates `("PASS","BLOCKED")` on persisted receipts;
  `readiness_item_contracts.py:5` has a 5-value status set. A literal scan for sets containing both `"succeeded"` and
  `"failed"` hits 17 files / 30 sites, including six orchestrator files that are distinct vocabularies
  (`ACCEPTED_PROJECTION_OUTCOMES`, array task outcomes, `PRODUCTION_STATUS_TAXONOMY`, provenance `ALLOWED_JOB_STATUSES`).
- Lint: `[tool.ruff.lint] select = ["E","F","I"]`; no mypy; 569 broad excepts, 269 `except: pass/continue` sites,
  `Any` in annotations per module 0–57 %.
- Frontend: `src/pages/hydroMet/bootstrap.ts` imported by seven production files (`stores/hydroMetProductData.ts`,
  `stores/stationLayerData.ts`, `components/map/M11StationForcingPopup.tsx`, `components/map/M11RiverForecastPanel.tsx`,
  `pages/OverviewPage.tsx`, `pages/m11/useHydroMetProduct.ts`, `pages/m11/useStationLayer.ts`) and by seven test files
  via import or `vi.mock('@/pages/hydroMet/bootstrap')`, plus `pages/hydroMet/__tests__/bootstrap.test.ts` importing it
  and reading the source by path (15 files); `stores/__tests__/overviewData*.test.ts` and `__tests__/AppRoutes.test.tsx` import `@/pages/*`
  helpers; no file-level import cycle exists today (the audit's cycles are directory-level); hand-typed
  `PipelineStatus`/`JobStatus` in `lib/constants.ts:10,18` with generated equivalents in `api/types.ts:1818,1835`;
  `failedStatuses` duplicated in `components/monitoring/BasinFailures.tsx:19` and `JobsTable.tsx:39`; one shared
  envelope `api/response.ts` (`ApiErrorShape`, `getApiErrorMessage`, `unwrapApiData`) beside four local normalizers
  (`safeM11ErrorMessage`, `getSafeModelAssetErrorMessage`, `apiErrorCode`, `settledError`);
  `tsconfig.app.json` is `strict` with unused checks; no ESLint anywhere; `check:api-types` exists as a script but the
  CI job runs only typecheck, check:types, build, test, check:bundle.
- Governance: `scripts/governance/entropy_audit/check_stale_routes.py::_path_is_route_authority_expanded_scope` already
  treats `openspec/changes/archive/` as expanded authority scope (which is why 665 findings land there);
  `tests/test_entropy_audit_baseline_writer_summary.py:76-77` pins `total_test_files == 247` and
  `total_instruction_files == 3` as literals read from the tracked baseline.
- Journal: 17 505 lines; `FileOrchestrationJournalRepository` ≈ 11 063 lines and `FileJournalRetryService` ≈ 1 377 lines
  stay; top-level functions total ≈ 3 479 lines, so the facade cannot shrink by more than ≈ 3 500 lines in this change.
  Tests patch on the facade: `_utcnow` (8 files), `read_bytes_limited_no_follow`, `list_directory_no_follow_limited`
  (imported into the facade from elsewhere, called by the Repository), `_stat_signature`, `_iter_regular_json_files`,
  `_iter_jsonl_files` (called by the Repository and by top-level path functions), and `_released_candidate_cycle_scope`,
  `_cycle_scope_from_job_id`, `_next_file_manual_retry_job_id_for_run` (also called from inside the Repository /
  RetryService classes, so all nine patched names stay in the facade). **No function-level cluster map exists yet**;
  Stage 2 only estimated cluster sizes. Selector: 48 `stop_on_match` rules; tests patch `PATH_TEST_RULES` (24×),
  `SUPPORT_MODULE_TEST_RULES`, several `*_ROOTS` and `_SUITE_IMPORTER_PARSE_STATS` on the facade;
  `openapi/nhms.v1.yaml` is hand-maintained (the selector pins it as such), `pnpm generate:api` regenerates `types.ts`.

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
- Any runtime behaviour change: every migrated status site keeps a set of identical content (one named set per site
  purpose, D3); no scheduler, persistence, upsert or parser predicate changes membership. The single accepted public
  delta is `reservation_lost` joining `schemas/pipeline_job.schema.json`, the API tuple and the generated frontend
  type, because the orchestrator already writes it (user decision: code is truth, schemas follow). That delta is a
  display-plane contract change and task 2.2 therefore carries a node-27 live receipt (CLAUDE.md oracle routing).
- Splitting `FileOrchestrationJournalRepository` itself (see Not yet specified).
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
The symbols `packages/common` needs move into `packages/common/contracts/` as four modules: `direct_grid.py` (the
whole of `direct_grid_contract.py`, 519 lines, which imports only `packages.common.source_identity`),
`time_normalization.py` (the nine pure helpers of `data_adapters/base.py` listed in the spec plus
`check_three_way_time_consistency` from `services/orchestrator/time_consistency.py`; the adapter base class and
dataclasses stay in workers), `region.py` (the 75-line region helpers) and `hydrologic_fingerprint.py` (the AST-computed
transitive closure of `verify_hydrologic_core_fingerprint_equal`: the `SpAttRewriteError` base, four exception classes,
`HydrologicCoreFingerprint`, `compute_hydrologic_core_fingerprint`, `_validate_category_files`, three constants and
whatever further private helpers the closure reaches; the rest of the 2 298-line rewrite algorithm stays in
`workers/mapping_builder`, re-exports the moved public names and inherits from the contract base). Each original module becomes
a re-export shim (explicit `from packages.common.contracts.x import a, b` plus `__all__`, five shims including
`time_consistency.py`) so every importer, including module-attribute access, keeps working (expand). After every
importer in the repository is migrated, the shims are deleted (contract); the contract step also repoints
`chain_compat_runtime`'s owner module and the identity assertions in `tests/test_orchestration_chain.py`, and adds
selector rule rows for the four contract modules equivalent to the rows that own the old paths (same test sets, same
`stop_on_match` semantics, in `PATH_TEST_RULES`); the guard/ratchet tests use the other mechanism, an additive loop in
`select_tests` after the stop-rule loop in the #1656/#2185 shape, with the affected exact-set assertions in
`tests/test_select_ci_tests.py` updated in the same PR as those precedents did. Alternative rejected: a new `packages/contracts` distribution, because
`packages/common` is already the shared layer and a second one adds a naming decision with no enforcement benefit.

### D2 Layering guard is a pytest over the AST (user)
`tests/test_import_layering_guard.py` walks every `.py` under `packages/` and fails on any `import`/`from` whose
top-level package is `workers`, `services`, `apps` or `scripts`, and every `.py` under `workers/` importing `services`
or `apps`. It runs in the normal targeted lane (it is selected whenever `packages/**` or `workers/**` changes, via
`scripts/select_ci_tests.py` guarded-owner rules) and in the full lane. An allowlist file
`tests/import_layering_allowlist.json` (`{file, module}` entries, deduplicated) lists the upward imports that still
exist; it starts with the current 12 sites and is `[]` as soon as the expand step lands, because shim re-exports are
downward imports; the test fails if an entry's import is absent from the file's AST (dead entries rot).
Alternative rejected: import-linter (new dependency, second config language).

### D3 Status vocabulary module and parity tests (user: code is truth, schemas follow)
`packages/common/status_vocab.py` defines **one named set per site purpose**, never a union across sites (the
Stage 3 review showed that a superset changes scheduler candidate construction, persistence guards, met_store upsert
and the output parser). Public closed sets are spelled out in the spec; the per-purpose sets and their canonical sites:

| Named set | Canonical site (members copied verbatim) | Other consumers that import it |
|---|---|---|
| `PIPELINE_JOB_STATUS_ORDER` / `PIPELINE_JOB_STATUSES` | `apps/api/routes/pipeline.py` tuple + `reservation_lost` | `schemas/pipeline_job.schema.json`, OpenAPI, `types.ts` |
| `PIPELINE_JOB_INTERNAL_STATUSES` | `accepted_submit_identity.py` (`reserved`, `cancellation_pending`, `reconcile_unverified`) | `ACCEPTED_SUBMIT_MASTER_STATUSES` derived as `(PIPELINE_JOB_STATUSES - {skipped}) \| INTERNAL` with a parity pin |
| `PIPELINE_JOB_TERMINAL` | `chain_types.py` (binds `set(...)` once, shared object) | `chain_runtime_utils.py`, `file_orchestration_journal.py` import the `chain_types` object |
| `PIPELINE_JOB_TERMINAL_WITHOUT_RESERVATION` | `scheduler.py` (binds `set(...)` once) | `scheduler_candidates.py` imports the `scheduler` object |
| `PIPELINE_JOB_TERMINAL_GUARD`, `PIPELINE_JOB_TERMINAL_OVERRIDES`, `PIPELINE_JOB_ACTIVE_MANUAL_RETRY` | `persistence.py` | — |
| `PIPELINE_JOB_SUCCESS` (`scheduler_state_types` binds `set(...)` once) | `scheduler_state_types.py` `TERMINAL_PIPELINE_SUCCESS_STATUSES` | `chain.py`, `chain_runtime_utils.py`, `chain_source_cycle.py`, `chain_repository_state.py`, `retry.py`, inline sites in journal / `persistence.py` / `forcing_submit_identity.py` |
| `PIPELINE_JOB_ACTIVE`, `PIPELINE_JOB_PENDING`, `PIPELINE_JOB_FAILED_UI` | `apps/api/routes/pipeline.py` `_ACTIVE/_PENDING/_FAILED_JOB_STATUSES` | — |
| `FAILED_PIPELINE_STATUSES`, `DURABLE_HYDRO_SUCCESS_STATUSES`, `HYDRO_RUN_CODE_CLEARING_STATUSES` | `scheduler_state_types.py` (moved as frozensets; `scheduler_state_types` binds `set(...)` for the two `set`-typed ones and the frozenset itself for the third, same names) | #1581 consumers unchanged |
| `HYDRO_RUN_STATUSES` | `schemas/run_status.schema.json` enum | parity ⊇ folded DB enum − `frequency_done` |
| `HYDRO_RUN_TERMINAL_UI` | `apps/api/routes/pipeline.py` `_TERMINAL_HYDRO_STATUSES` | — |
| `DISPLAY_PRODUCT_READY_STATUSES` (site binds `set(...)`) | `hydro_display_constants.py` | — |
| `QHH_LATEST_READY_RUN_STATUSES` (`tuple`) | `forecast_store.py` | — |
| `PARSE_READY_RUN_STATUSES`, `FAILABLE_RUN_STATUSES` (`tuple`, DB parameters) | `workers/output_parser/parser.py` | — |
| `FORECAST_CYCLE_SUCCESS_STATUSES` | `file_orchestration_journal.py` `_TERMINAL_FORECAST_CYCLE_SUCCESS_STATUSES` | — |
| `CYCLE_STATUSES` | folded `met.cycle_status` migrations | — |
| `CYCLE_TERMINAL_UI` | `apps/api/routes/pipeline.py` `_TERMINAL_CYCLE_STATUSES` | — |
| `CYCLE_TERMINAL_UPSERT`, `CYCLE_UPSERT_PRESERVED` (`tuple`, concatenated) | `packages/common/met_store.py` | — |
| `SLURM_TERMINAL_STATES` | `slurm_validation.py` | `SLURM_RAW_STATES` = this ∪ `real_backend.SLURM_STATE_MAP` keys ∪ `run_qhh_continuous` sets |
| `SLURM_TERMINAL_SUCCESS`, `SLURM_TERMINAL_FAILURE` | `scripts/run_qhh_continuous.py` | — |
| `QC_VERDICTS` + `normalize_qc_verdict` | new (lowercase) | read/compare paths in `production_closure/*`, `scripts/*` |
| `READINESS_ITEM_STATUSES` | `readiness_item_contracts.py` | — |

Vocabulary objects are immutable; `set`-typed sites bind `set(...)` once in the module that owns the shared object today, so every site keeps its type and identity (the parity test checks both). The vocabulary provides an ordered tuple for every set that reaches a schema or OpenAPI enum (`PIPELINE_JOB_STATUS_ORDER`,
`HYDRO_RUN_STATUS_ORDER`, `CYCLE_STATUS_ORDER`) so generated enums are deterministic under hash randomisation; frozensets
and `Literal` aliases derive from the tuples. `tests/test_status_vocab_parity.py` asserts: each named set equals the
literal pinned from its canonical site at `f7be77f22`; `PIPELINE_JOB_STATUS_ORDER == schemas/pipeline_job.schema.json`
enum (ordered); `HYDRO_RUN_STATUSES == schemas/run_status.schema.json` enum and ⊇ the migration-folded `hydro.run_status`
enum minus `frequency_done` (reusing the folding helper of `tests/test_hydro_status_set_parity.py`, the #1581 oracle, so
there is one folding implementation); `CYCLE_STATUSES ==` folded `met.cycle_status`; the duplicate-literal scan (excluding `status_vocab.py` itself) with an allowlist keyed by `file` + sorted literal
members and a reason (`pending-migration | distinct-vocabulary | not-a-status-set`) whose exit condition is "no
`pending-migration` entry"; the initial hit list is produced by running the widened scan at introduction and
adjudicating every hit. `normalize_qc_verdict` is read-side only: receipt writers and validators keep their spelling
and are allowlisted `distinct-vocabulary`. `reservation_lost` is the only value added to any schema; `complete`/`published`
are not added anywhere (no pipeline-job writer emits them; they stay in the read-side `PIPELINE_JOB_SUCCESS`).
Alternative rejected: eight union sets (Stage 2 draft) — four review findings showed it changes runtime behaviour.

### D4 Helper consolidation lands after D1 (user)
Targets: utc-now → `packages/common/contracts/time_normalization.py::utc_now`; ISO parse → `parse_iso_utc`; format →
`format_iso_utc`; source normalizer → existing `packages/common/source_identity.py::normalize_source_id`. One batch per
top-level package (`packages`, `services`, `workers`, `apps`, `scripts`); each batch replaces local definitions with
imports and deletes the local definition; a count test `tests/test_time_helper_consolidation.py` asserts the number of
module-level functions whose name `re.fullmatch`es the spec's pattern list (`_?utc_?now|_?now_iso|_?iso_now|
_?parse_(iso|utc|time|iso8601|iso_utc)|_?coerce_datetime|_?format_time|_?normalize_source(_id)?`) outside the canonical
modules does not exceed a ceiling file that each batch lowers; the initial PR attaches the hit list (≈70 by fullmatch;
prefix matching would wrongly include ≈19 different-concept helpers such as `_normalize_sources(values, env)`). Locals
that are a different concept are renamed to say so and allowlisted with a reason. The ceiling, `Any` ratchet and status
parity tests get additive-loop routing (#1656 shape) for `apps|packages|services|workers|scripts/**` so they run on PRs
(targeted lane) even when a `stop_on_match` rule owns the changed path, not only on master.

### D5 Lint ratchet: ruff rules with frozen inventory plus `Any` count ceiling (user)
`pyproject.toml` `[tool.ruff.lint] select` gains `"B"` and `"BLE"`; `[tool.ruff.lint.per-file-ignores]` lists every file
that currently violates, generated by `ruff check --select B,BLE --output-format json` at the time of the change and
committed; CI already runs `uv run ruff check .`, so new violations fail. `tests/test_any_ratchet.py` counts `Any`
occurrences in annotations (AST: `Name` `Any` or `Attribute` ending in `.Any`, string annotations parsed first; the audit
scanner is not in the repository, so the test's own method is the definition) over `apps/ packages/ services/ workers/ scripts/`,
compares to `tests/any_ratchet_ceiling.json` (`{"ceiling": <n>}`), fails above the ceiling, and fails when the count is
more than 5 % below the ceiling (forcing the ceiling to be lowered in the same PR so the ratchet does not slacken).
`ANN401` stays off (open question).

### D6 Frontend: three-rule ESLint, leaf-only pages, generated statuses, one normalizer (user)
`eslint.config.js` (flat; `typescript-eslint`, `eslint-plugin-import-x`, `eslint-import-resolver-typescript` so `@/`
resolves) with `@typescript-eslint/no-explicit-any`, `import-x/no-cycle`, and `no-restricted-imports` forbidding
`@/pages/*` and relative `pages/` imports from `src/stores`, `src/components`, `src/lib`, `src/api`, `src/hooks`, with
test files (`**/__tests__/**`, `*.test.*`) ignored by the restricted-imports block only (three existing tests import
page helpers). `package.json` gains `"lint": "eslint src"`; `.github/workflows/ci.yml` `frontend-build` runs
`pnpm check:api-types` (its own one-line task, no dependency) and `pnpm lint` after typecheck. `src/pages/hydroMet/bootstrap.ts`
moves to `src/stores/hydroMetBootstrap.ts` (same exports); seven production importers and eight test files change only
their path strings. There is no file-level cycle today, so the cycle rule's oracle is a negative test (an injected `@/`
cycle must fail lint). `lib/constants.ts`
`PipelineStatus`/`JobStatus` become aliases of the generated `components["schemas"]` types; `failedStatuses` becomes one
exported constant in `lib/constants.ts`. API failure handling: every fetch site in `src/` resolves errors through
`api/response.ts::getApiErrorMessage` (three local normalizers become thin wrappers or are deleted; `apiErrorCode`
parses the shared envelope shape); `useNationalBasinGeo` gains an error carrier (`NationalBasinGeoState.error:
string | null`, failed fetch not cached) and the uncalled `lib/mvtLayerMetadata.ts` loader is deleted instead of being
given one, with no change to what the UI renders when data is absent (`settledError` lives in the bootstrap module, so this lands after its move). Alternative
rejected: Biome (another toolchain for three rules).

### D7 Decomposition by responsibility cluster, facade-preserving (user)
For each of `file_orchestration_journal.py` and `select_ci_tests.py`: one task per cluster moves that cluster's
definitions to a sibling module (`services/orchestrator/file_journal_<cluster>.py`, `scripts/select_ci_tests_<cluster>.py`
or a `scripts/ci_selector/` package), the original file re-exports every moved name that is **imported** so importers
keep passing unchanged; the final task deletes the re-exports once an AST scan shows no user and the facade shrinks to
the code the cluster map assigns to it. Patched seams follow the base requirement "Relocated monkeypatch seams keep
their negative oracles live": a patched name moves only with all its call sites (tests repoint, no re-export); a patched
name with a call site that stays in the facade (the Repository and RetryService classes stay) stays too, and moved
callers reach it through a late `from . import file_orchestration_journal` inside the function body, so the facade
patch remains the single interception point and no import-time cycle appears. Because no function-level cluster map
exists yet, the first journal task produces it as a checked-in fixture with a test; extraction tasks consume it and the
contract oracle is "facade top-level definitions == map's `facade` cluster", not a line-count target (the arithmetic
ceiling is ≈ 3 500 lines). For the selector the same rule applies to `_SUITE_IMPORTER_PARSE_STATS` (moves with the
import-graph functions, one allowed test repoint), while table/rule globals that tests patch on the facade stay bound
there and moved functions receive them as parameters from `select_tests`. Behaviour oracle per step: the existing test
files pass, and for the selector `uv run python scripts/select_ci_tests.py` over a fixed changed-path list yields
byte-identical output at the PR's parent commit and head (no committed snapshot: parallel tasks add rule rows and would
invalidate one). Extraction order is in tasks.md §5/§6; the highest-risk step is flagged there.

### D8 Governance noise and baseline (user)
`check_stale_routes.py::_check_stale_route_tokens` skips paths under `openspec/changes/archive/` (one path-family
predicate local to that check; `_path_is_route_authority_expanded_scope` is not changed for `docs/archived/` or
`docs/runbooks/`). `tests/test_entropy_audit_baseline_writer_summary.py` drops the two literal assertions on the tracked
file's counts (they are tautological once read from the file and would fail against the HEAD inventory the moment a
test file is added), asserts the tracked values are positive integers and `total_modules == len(modules)`, and keeps the existing
fresh-baseline-vs-HEAD-inventory comparison (`v1_summary_* == summary`). The `skipped_path_families` list stays a flat
string list (writer compatibility) and gains one scoped entry. Then `uv run python scripts/governance/write_entropy_baseline.py` is run once and the
refreshed `latest.json` plus the archived previous snapshot are committed.

### D9 Scoped AGENTS.md (user: all nine in one issue)
`packages/common`, `services/precip`, `workers/model_registry`, `workers/data_adapters`,
`workers/forcing_producer`, `workers/mapping_builder`, `workers/grid_registry`, `services/slurm_gateway`, `scripts`
(the nine Context-🔴 rows of audit §1; `packages/scheduler` has 3 files and is 🟡, so it is not in the set) each get an AGENTS.md with
the four sections the existing scoped files share (`Required Reading`, an ownership/boundary section, a fences or
error-model section, `Focused Verification`), each verification command runnable locally. The governance docs gate is
the oracle. One issue, `Width exception: merged-tasks`, because the files are independent but share one author pass,
one verification path and no code.

### D10 Legacy store reference retirement (fact-check: table dropped 2026-09-19)
`packages/common/node27_pgdata_workload_plan.py` drops `LEGACY_HYPERTABLE` from the SQL `IN` list, the default
`expected_relations` and the candidate loader's parent set (a legacy-parent candidate is then refused as
`PLAN_UNRELATED_PARENT`; the one loader test that expected it to load is rewritten); `node27_timeseries_discovery.py` keeps its catalog-driven derivation (it only applies when present);
runbook/spec text (three runbooks/ADR and the five specs named in the spec delta) gets the DOC_STATUS section marker or
the sentence rewritten to past tense. `display_coverage.py::_REFUSAL_ADVICE` is operator text that names the dropped
table as history and is allowlisted by name in the string test (rewording it would change a pinned message).

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
- D3 adds `reservation_lost` to the pipeline-job schema, API tuple and generated type: validators accept one more
  value than before. Whether the API already returns such rows is verified on node-27 in task 2.2's receipt.
- D3 allowlist adjudication (every hit of the widened scan) is manual work in task 2.1; a wrong `not-a-status-set` tag hides a copy.
- D5 freezes a large `per-file-ignores` list; it is generated, not hand-written, and shrinks only by cleanup PRs.
- D6 `import/no-cycle` on a 115-file tree is the slowest rule; measured impact belongs in the issue's evidence.
- D7 touches a 17.5 k-line file that receives ~1.7 commits/day; each extraction PR must rebase quickly or conflict.
  Mitigation: small clusters, one PR at a time, facade keeps names stable; the status (2.3) and `_utcnow` (3.3) edits
  to the same file land before the first extraction.
- D3/D6 widen the public enum, so task 2.2 needs a node-27 live receipt; everything else is local.

## Migration Plan

Expand–contract for D1 (shims) and D7 (facades). Everything else is additive or a pure move with tests. No data
migration, no deployment step; the only node-27 receipt is for task 2.2 (public enum gains `reservation_lost`); the other
frontend changes are structural and are covered by the mocked-regression lane plus typecheck.

## Open Questions

- Q1: when to enable `ANN401` (after the `Any` ratchet has reached a threshold, or never, relying on the ratchet).
- Q2: whether `scripts/` should be split into a package (`scripts/ci_selector/`) or stay flat files for the selector
  siblings; tasks.md picks the flat layout and the implementer may propose the package if the sibling count exceeds four.

## Not yet specified

- A type checker roll-out (mypy/pyright): the problem (which modules first, strictness) is not stated precisely enough.
- Splitting `FileOrchestrationJournalRepository` (≈ 11 k lines) and `FileJournalRetryService`: they stay in the facade;
  the cluster map produced by task 5.0 is the input for a later change.
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
