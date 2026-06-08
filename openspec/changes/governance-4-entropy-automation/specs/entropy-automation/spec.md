## ADDED Requirements

### Requirement: Entropy audit produces repeatable reports

The repository SHALL provide a script that scans entropy signals and emits both machine-readable JSON and human-readable Markdown reports.

#### Scenario: maintainer runs JSON report
- **WHEN** `uv run python scripts/governance/audit_repo_entropy.py --format json` is run
- **THEN** the output includes module scores, role classification, high-spread patterns, and cleanup priorities

#### Scenario: maintainer runs Markdown report
- **WHEN** `uv run python scripts/governance/audit_repo_entropy.py --format markdown` is run
- **THEN** the output includes an entropy heatmap and prioritized cleanup targets

#### Scenario: report includes six-axis and governance-face schema
- **WHEN** either report format is generated
- **THEN** findings include structure/semantics/behavior/context/protocol/control scoring or axis attribution, `governance_face`, `role`, `evidence_path`, `severity`, `priority`, `owner_area`, and optional allowlist reason

#### Scenario: report covers required check families
- **WHEN** the audit script runs in non-blocking mode
- **THEN** it reports checks for role/env boundaries, diagnostic tokens, paused `&& false` workflow jobs, mocked-vs-live e2e broad mocks, stale route/doc tokens, placeholder paths, Makefile/toolchain discipline, OpenAPI/frontend type drift, standalone gateway business-route leakage, tracked agent/artifact ownership, and `apps.api.*` layer inversion outside API code

### Requirement: Initial governance CI is non-blocking

The first governance automation workflow SHALL report entropy findings without failing PRs for known existing issues.

#### Scenario: known legacy token exists during initial rollout
- **WHEN** the governance workflow detects an existing legacy token
- **THEN** it records the finding in the report but does not fail CI during non-blocking rollout

### Requirement: Stable role-boundary violations can become hard gates

After baseline cleanup, selected stable invariants SHALL be eligible for hard-fail enforcement.

#### Scenario: display env includes compute-only env
- **WHEN** a display env or compose file contains compute-only env such as `WORKSPACE_ROOT`, `SHUD_EXECUTABLE`, or `SLURM_GATEWAY_URL`
- **THEN** the hard-gate mode fails the check

#### Scenario: live e2e uses broad API mock
- **WHEN** a live e2e spec contains `page.route('**/api/v1/**')`
- **THEN** the hard-gate mode fails the check

#### Scenario: production orchestration references diagnostic QHH scripts
- **WHEN** production scheduler/orchestrator code references QHH diagnostic script tokens
- **THEN** the hard-gate mode fails the check

#### Scenario: standalone gateway route leakage is detected
- **WHEN** the standalone Slurm gateway app registers forecast, model, pipeline, static, or frontend routes
- **THEN** the hard-gate mode fails the check

#### Scenario: OpenAPI and generated frontend types drift
- **WHEN** generated frontend types do not match `openapi/nhms.v1.yaml`
- **THEN** the hard-gate mode fails or delegates to the existing contract drift gate and reports the failure under `shared_contract`

#### Scenario: paused CI job uses hidden false condition
- **WHEN** a workflow job is disabled by `&& false`
- **THEN** the hard-gate mode fails after the non-blocking rollout period

#### Scenario: Makefile command discipline regresses
- **WHEN** Makefile Python/lint targets invoke system `python`, `pytest`, or `ruff` instead of `uv run`
- **THEN** the hard-gate mode fails after Governance-0 has landed

#### Scenario: tracked agent or frontend artifact ownership drifts
- **WHEN** `.agents`, `.codex`, or frontend artifact paths conflict with the documented ownership policy
- **THEN** the hard-gate mode fails after Governance-3 has landed

### Requirement: Entropy baseline writing is explicit

The audit tooling SHALL NOT create or update `.entropy-baseline/latest.json` unless explicitly requested by a maintainer.

#### Scenario: audit runs in report mode
- **WHEN** the audit script runs without a baseline write flag
- **THEN** it does not create or modify `.entropy-baseline/latest.json`
