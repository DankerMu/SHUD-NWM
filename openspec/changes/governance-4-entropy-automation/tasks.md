## 0. Dependency gate

- [x] 0.1 Confirm `governance-0-ci-contract-baseline` is merged and green.
  Evidence: #353 is closed; baseline PRs #375 and #376 are merged.
- [x] 0.2 Confirm Governance-1/2/3 have landed or provide explicit known-finding allowlist entries for the report-only rollout.
  Evidence: #354, #355, and #356 are closed; Governance-3 child PRs #387,
  #388, #390, and #391 are merged.
- [x] 0.3 #371 completion boundary: this PR completes sections 0-1 only.
  Sections 2-4 are future slices for #372, #373, and #374 and must not be
  implemented in #371.

## 1. Audit script

- [x] 1.1 Add `scripts/governance/audit_repo_entropy.py` with JSON and Markdown output modes.
- [x] 1.1a Verify JSON mode:
  `uv run python scripts/governance/audit_repo_entropy.py --format json`.
  Output must parse as JSON and contain `metadata`, `module_heatmap`,
  `findings`, and `high_spread_patterns`.
  Evidence: `UV_NO_SYNC=1 uv run python scripts/governance/audit_repo_entropy.py --format json`
  parsed as JSON with 351 findings and 26 heatmap rows. Plain `uv run python ...`
  timed out locally while waiting for `.venv/.lock` held by an existing uvicorn
  service; no script process hung when using the repository `.venv`.
- [x] 1.1b Verify Markdown mode:
  `uv run python scripts/governance/audit_repo_entropy.py --format markdown`.
  Output must include a six-axis heatmap table and prioritized cleanup targets.
  Evidence: `uv run --no-sync python scripts/governance/audit_repo_entropy.py --format markdown`
  emitted `## Entropy Heatmap` and `## Prioritized Cleanup Targets`.
- [x] 1.2 Implement checks for role boundary, legacy/dead-code, docs alignment,
  protocol/control drift, OpenAPI/frontend type drift, paused CI jobs,
  Makefile/toolchain discipline, tracked agent/artifact ownership, standalone
  gateway route leakage, and layer inversion imports.
- [x] 1.2a Required report-only check families:
  - role/env boundaries for display vs compute env/compose files.
  - `DIAGNOSTIC-ONLY` and QHH diagnostic token references in production paths.
  - paused workflow conditions such as `&& false`.
  - broad `page.route('**/api/v1/**')` mocks in live-looking e2e paths.
  - stale route/doc tokens such as `/hydro-met` and `HydroMetPage`.
  - placeholder paths such as `apps/web`, hyphenated workers,
    `workers/sbatch_templates`, and `services/tile-publisher`.
  - Makefile direct `python`, `pytest`, or `ruff` command discipline drift.
  - OpenAPI/generated frontend type drift signal or delegation to existing
    contract-drift checks.
  - standalone Slurm gateway route leakage into forecast/model/pipeline/static
    business routes.
  - tracked agent/artifact ownership drift against `DOC_STATUS.md`.
  - `apps.api.*` layer inversion imports outside the API layer.
  Evidence: report metadata includes `executed_check_families` for all required
  families; `tests/test_entropy_audit_script.py` now includes temporary-repo
  positive-signal coverage for required families, including generic display
  compose detection and standalone Slurm gateway path/decorator leakage.
- [x] 1.3 Include module-level heatmap fields: structure, semantics, behavior,
  context, protocol, control, priority.
  Evidence: `tests/test_entropy_audit_script.py::test_entropy_audit_json_schema_is_stable`.
- [x] 1.4 Include finding fields: `governance_face`, `role`, `evidence_path`,
  `severity`, `priority`, `owner_area`, and optional allowlist reason.
  Evidence: `tests/test_entropy_audit_script.py::test_entropy_audit_json_schema_is_stable`.
- [x] 1.5 Verify default report mode does not write a baseline:
  `test ! -e .entropy-baseline/latest.json` before and after JSON/Markdown
  report runs, or record explicit pre-existing baseline state without modifying
  it.
  Evidence: before/after `test ! -e .entropy-baseline/latest.json` returned 0
  after JSON and Markdown report runs.
- [x] 1.6 Verify the script skips large generated/vendor/runtime trees such as
  `.git`, `.venv`, `node_modules`, `dist`, root `artifacts`, `data`, and
  `.nhms-*`.
  Evidence: metadata `skipped_path_families` records these skip families; file
  iteration prunes skipped directories before descent. The focused test keeps
  root `artifacts/` and `data/` skipped while proving source packages such as
  `services/artifacts/*.py` remain scannable.
- [x] 1.7 Add focused tests if the script logic is non-trivial enough that JSON
  schema, check classification, or no-baseline-write behavior could regress.
- [x] 1.7a Add or run a schema validation test that asserts stable JSON field
  names: top-level `metadata`, `module_heatmap`, `findings`,
  `high_spread_patterns`; heatmap axes `structure`, `semantics`, `behavior`,
  `context`, `protocol`, `control`, `priority`; finding fields
  `governance_face`, `role`, `evidence_path`, `severity`, `priority`,
  `owner_area`, and optional `allowlist_reason`.
  Evidence: `uv run --no-sync pytest -q tests/test_entropy_audit_script.py`
  returned `29 passed`, including fixed-path symlink regressions for
  `Makefile` and `docs/governance/DOC_STATUS.md`.
- [x] 1.7b Add or run a no-baseline-write test that executes JSON and Markdown
  modes in a temporary or real repo context and proves `.entropy-baseline/latest.json`
  is not created or modified unless an explicit baseline write flag is used.
  Evidence: `uv run --no-sync pytest -q tests/test_entropy_audit_script.py`
  returned `29 passed`.

## 2. Report docs

Out of scope for #371. Owned by #372.

- [ ] 2.1 Add `docs/governance/entropy-budget.md` defining non-blocking vs hard-gate stages.
- [ ] 2.2 Add `docs/governance/entropy-report.example.md` showing expected report shape.
- [ ] 2.3 Document that `.entropy-baseline/latest.json` is not written without explicit confirmation.

## 3. Non-blocking CI

Out of scope for #371. Owned by #373.

- [ ] 3.1 Add a governance workflow or CI job that runs the audit in non-blocking report mode.
- [ ] 3.2 Upload or print the Markdown/JSON report without failing PRs for known baseline findings.
- [ ] 3.3 Verify workflow execution on a branch and include report evidence in PR body.

## 4. Hard-gate preparation

Out of scope for #371. Owned by #374.

- [ ] 4.1 Add CLI flags or config for hard-gate mode.
- [ ] 4.2 Prepare hard-gate checks for display compute-only env, production diagnostic token references, live e2e broad mocks, standalone gateway business route leakage, OpenAPI/frontend type drift, paused CI jobs, Makefile command discipline, and tracked agent/artifact ownership.
- [ ] 4.3 Keep hard-gate mode disabled in CI until Governance-0 through Governance-3 are complete or explicitly waived.
