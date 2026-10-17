# Proposal: entropy-burndown-2026-10

## Why

The 2026-10-16 repo entropy audit (`docs/governance/entropy-audit-2026-10-16.md`, snapshot
`docs/governance/entropy-audit-2026-10-16.snapshot.json`) measured the code tree at `f7be77f22` and found entropy that is
still being replicated by every new change:

- **Structure**: `packages/common` imports `workers.*` at 11 sites (7 files), forming a five-module import cycle with
  `workers/{canonical_converter,data_adapters,forcing_producer,mapping_builder}`; one site was added on 2026-09-24.
  `workers/shud_runtime/runtime.py` imports `services.orchestrator.time_consistency`. Two files dominate churn:
  `services/orchestrator/file_orchestration_journal.py` (17 506 lines, 103 commits in 60 days) and
  `scripts/select_ci_tests.py` (7 623 lines).
- **Semantics**: the pipeline-job terminal set is hand-copied in 8 files and disagrees (`reservation_lost` is produced and
  consumed by 16 orchestrator files but absent from `schemas/pipeline_job.schema.json` and the API tuple); the success
  set `{succeeded, complete, published}` is copied 6 times; run status differs between DB enum, JSON schema and code; QC
  verdicts are spelled `passed`/`pass`/`PASS`/`BLOCKED`; 30 utc-now helpers, 28 ISO parsers, 52 time formatters and 13
  local source normalizers exist beside shared ones; the retired `hydro.river_timeseries_legacy` is still executed in
  `packages/common/node27_pgdata_workload_plan.py`.
- **Behavior**: `Any` is 10–32 % of annotations in 15 of 22 Python modules with no type checker; 569 broad excepts and
  269 silent swallows; 756 lines containing `Any` and 31 new broad excepts were added in the last 30 days. ruff selects
  only `E,F,I`.
- **Frontend**: `pages/hydroMet/bootstrap.ts` is a data module imported upward by `stores/` and `components/`, creating two
  directory cycles; `PipelineStatus`/`JobStatus` are hand-typed although generated equivalents exist; `failedStatuses` is
  copied verbatim; API errors go through 3 transports and 6 normalizers; there is no ESLint; `check:api-types` is not in
  the CI job.
- **Context/Control**: 9 critical modules have no scoped AGENTS.md; 665 of 834 governance findings sit in
  `openspec/changes/archive`, which DOC_STATUS classifies as historical evidence; the tracked entropy baseline is pinned
  by literal counts in a test, so it cannot be refreshed without editing the test.

## What Changes

- Extract the contract subset that `packages/common` needs from the four workers into `packages/common`, leave
  re-export shims in the workers, add a pytest AST guard that forbids `packages/` from importing `workers|services|apps`,
  then contract the shims.
- Create `packages/common/status_vocab.py` as the single source for pipeline-job terminal/success sets, hydro run
  status, cycle status, QC verdict and Slurm raw states, with parity tests against `schemas/*.json` and
  `db/migrations/000003_enums.sql`; add the runtime-real values (`reservation_lost`, `complete`, `published`) to the
  schemas and the API tuple; migrate the copied sets to import from it.
- Consolidate utc-now / ISO parse / time format / source-normalizer helpers onto the shared ones, one top-level package
  per batch.
- Lint ratchet: enable ruff `B` and `BLE001` with the existing violations frozen in `per-file-ignores`; add an `Any`
  count ratchet test whose ceiling is the current count and may only go down.
- Decompose `file_orchestration_journal.py` and `select_ci_tests.py` by responsibility cluster into sibling modules,
  keeping each original as a re-export facade until the last importer moves, then contract.
- Frontend: relocate `pages/hydroMet/bootstrap.ts` under `stores/`, replace hand-typed status types with generated
  aliases and a single `failedStatuses`, route API failures through one normalizer, add ESLint with three rules
  (`no-explicit-any`, `import/no-cycle`, `no-restricted-imports` against upward imports into `pages/`), and run
  `lint` plus `check:api-types` in the `frontend-build` CI job.
- Remove the `hydro.river_timeseries_legacy` reference from live code and retired-store wording from active runbooks
  and specs.
- Skip `openspec/changes/archive/` in the `stale-display-route-token` check only.
- Add scoped AGENTS.md files for the nine critical modules without one, following the existing four-section template.
- Make the baseline pin test read counts from the tracked baseline file, then refresh `.entropy-baseline/latest.json`
  with the repository writer.

## Capabilities

### New Capabilities
- `import-layering-guard`: `packages/` never imports upward; contract subsets live in `packages/common`; shims are temporary.
- `status-vocabulary`: one importable source for status/verdict sets with schema and DB-enum parity tests.
- `time-helper-consolidation`: one utc-now, one ISO parser, one formatter, one source normalizer per concept.
- `lint-ratchet`: ruff `B`/`BLE001` with frozen inventory and an `Any` count ceiling that only decreases.
- `frontend-lint-and-layering`: ESLint three-rule gate, `pages/` is leaf-only, generated status types, one API error normalizer.
- `scoped-agent-instructions`: scoped AGENTS.md for the nine critical modules.
- `legacy-store-reference-retirement`: no live code or active runbook/spec treats `hydro.river_timeseries_legacy` or `timeseries_store` as current.

### Modified Capabilities
- `orchestrator-structural-burndown`: adds the journal decomposition requirements (facade-preserving cluster extraction, then contract).
- `ci-test-selector-routing`: test-list data tables and selection logic become separate modules with unchanged selection output.
- `entropy-automation`: `stale-display-route-token` skips `openspec/changes/archive/`.
- `governance-entropy-baseline`: the baseline pin test derives its expected counts from the tracked baseline instead of literals.

## Impact

- `packages/common` (new contract modules, `status_vocab.py`, helper consolidation), `workers/*` (shims, then removal),
  `services/orchestrator` (journal siblings, status imports), `scripts/` (selector split, helper consolidation),
  `apps/api` (status tuple, schema), `schemas/pipeline_job.schema.json`, `schemas/run_status.schema.json`,
  `openapi/nhms.v1.yaml` where status enums are exposed, `pyproject.toml` ruff config, `apps/frontend` (eslint config,
  `package.json`, `src/`), `.github/workflows/ci.yml` frontend job, `scripts/governance/entropy_audit/`,
  `tests/test_entropy_audit_baseline_writer_summary.py`, `.entropy-baseline/latest.json`, nine new `AGENTS.md`.
- No runtime behaviour change is intended anywhere; every refactor task carries a behaviour-preservation oracle (existing
  tests plus a before/after output comparison where named).
