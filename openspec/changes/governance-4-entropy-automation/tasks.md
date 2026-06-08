## 0. Dependency gate

- [ ] 0.1 Confirm `governance-0-ci-contract-baseline` is merged and green.
- [ ] 0.2 Confirm Governance-1/2/3 have landed or provide explicit known-finding allowlist entries for the report-only rollout.

## 1. Audit script

- [ ] 1.1 Add `scripts/governance/audit_repo_entropy.py` with JSON and Markdown output modes.
- [ ] 1.2 Implement checks for role boundary, legacy/dead-code, docs alignment, protocol/control drift, OpenAPI/frontend type drift, paused CI jobs, Makefile/toolchain discipline, tracked agent/artifact ownership, standalone gateway route leakage, and layer inversion imports.
- [ ] 1.3 Include module-level heatmap fields: structure, semantics, behavior, context, protocol, control, priority.
- [ ] 1.4 Include finding fields: `governance_face`, `role`, `evidence_path`, `severity`, `priority`, `owner_area`, and optional allowlist reason.

## 2. Report docs

- [ ] 2.1 Add `docs/governance/entropy-budget.md` defining non-blocking vs hard-gate stages.
- [ ] 2.2 Add `docs/governance/entropy-report.example.md` showing expected report shape.
- [ ] 2.3 Document that `.entropy-baseline/latest.json` is not written without explicit confirmation.

## 3. Non-blocking CI

- [ ] 3.1 Add a governance workflow or CI job that runs the audit in non-blocking report mode.
- [ ] 3.2 Upload or print the Markdown/JSON report without failing PRs for known baseline findings.
- [ ] 3.3 Verify workflow execution on a branch and include report evidence in PR body.

## 4. Hard-gate preparation

- [ ] 4.1 Add CLI flags or config for hard-gate mode.
- [ ] 4.2 Prepare hard-gate checks for display compute-only env, production diagnostic token references, live e2e broad mocks, standalone gateway business route leakage, OpenAPI/frontend type drift, paused CI jobs, Makefile command discipline, and tracked agent/artifact ownership.
- [ ] 4.3 Keep hard-gate mode disabled in CI until Governance-0 through Governance-3 are complete or explicitly waived.
