## Issue Ownership

Issue #400 owns only task group 1 and the #400 evidence rows in task group 5. It is a
compact documentation/governance slice and must not change audit script logic,
runtime code, CI workflows, or node-27 frontend implementation files.

Issue #401 owns task group 2 and the shared verification rows needed for schema,
hard-gate, JSON/Markdown, and baseline non-write behavior. Issue #402 owns task
group 3 and tracked retired-path guard verification. #403 owns task group 4
after #401/#402 land and must stay documentation/example-only.

## 1. Baseline Triage Contract

- [x] 1.1 Capture the current Governance-4 report counts and classify each high-spread family as `fix`, `historical`, `archived`, `false-positive`, or `defer`.
- [x] 1.2 Document non-goals: no QHH diagnostic deletion, no repeated Governance-2 placeholder deletion, no CI hard-gate enablement.
- [x] 1.3 Add or update a governed triage artifact that maps the report families to downstream Governance-5 changes.

## 2. Audit Schema Semantics

- [x] 2.1 Add normalized finding fields for allowlist state, allowlist key, budget countability, and gate eligibility.
- [x] 2.2 Update hard-gate evaluation to count only gate-eligible findings instead of whole check families.
- [x] 2.3 Add summary counts by `check_id`, priority, role, allowlist state, and gate eligibility.
- [x] 2.4 Preserve report-only behavior and verify `.entropy-baseline/latest.json` is not created or updated.
- [x] 2.5 Add focused tests for equivalent allowlist wording, deterministic mocked evidence not being gate-eligible, live broad API mocks being gate-eligible, and parseable hard-gate JSON.

## 3. Retired Path Return Guard

- [x] 3.1 Add a tracked-file guard for retired active-tree paths: `apps/web`, hyphenated worker placeholders, `workers/sbatch_templates`, and `services/tile-publisher`.
- [x] 3.2 Distinguish tracked retired path reintroduction from historical text references in archived docs or governance inventories.
- [x] 3.3 Add focused tests proving a temporary reintroduced retired path is reported or fails the guard.
- [x] 3.4 Add focused tests proving untracked filesystem-only retired paths are not treated as tracked reintroductions.
- [x] 3.5 Add focused tests proving tracked-path discovery is scoped to the retired prefixes and does not flag normal active underscore worker/package paths.
- [x] 3.6 Add focused tests proving missing or unavailable git metadata does not crash report generation and does not create tracked-path-return false positives.

Expected #402 test inputs and outputs:

- Tracked `apps/web/README.md` in a temporary git repo -> one retired-path-return finding with #401 normalized fields.
- Force-added ignored `workers/shud-runtime/README.md` or equivalent hyphenated retired worker file -> one retired-path-return finding with #401 normalized fields.
- `docs/archived/**` or completed Governance-2 OpenSpec text containing retired path names -> only governed text-reference/placeholder semantics, no retired-path-return finding.
- Untracked filesystem-only `apps/web/README.md` -> no retired-path-return finding.
- Active underscore paths such as `workers/shud_runtime/__init__.py` -> no retired-path-return finding.
- Non-git or unavailable-git metadata root -> report construction succeeds in report mode and emits no retired-path-return false positive.

#402 non-goals:

- No node-27/frontend old page retirement, URL handoff, Playwright mocked-spec migration, M15 visual lane changes, or frontend implementation files.
- No CI hard-gate enablement, baseline writes, retired path deletion, or Governance-5 #403 report-example refresh.

## 4. Documentation And Report Example

- [x] 4.1 Update `docs/governance/entropy-budget.md` with E1 burn-down semantics and the continued report-only CI policy.
- [x] 4.2 Update `docs/governance/entropy-report.example.md` to include normalized allowlist, budget, and gate eligibility fields.
- [x] 4.3 Verify the example schema against live JSON output.
- [x] 4.4 Confirm `.github/workflows/governance.yml` remains report-only and does not pass `--mode hard-gate`.
- [x] 4.5 Document that tracked retired active-tree files are separate from governed historical/archive/OpenSpec text evidence.
- [x] 4.6 Confirm #403 stays documentation/example-only: no audit script changes, no CI workflow changes, no node-27/frontend/API/layer-inversion work, and no committed baseline.

Expected #403 documentation and evidence outputs:

- Live JSON report exits 0 and exposes documented metadata fields: `summary_counts`, `budget_counted_count`, `gate_eligible_count`, scan byte limits, skipped path families, `module_heatmap`, `findings`, and `high_spread_patterns`.
- Live JSON findings expose documented finding fields: `allowlist_state`, `allowlist_key`, `budget_counted`, and `gate_eligible`.
- Live JSON `summary_counts` includes `by_check_id`, `by_priority`, `by_role`, `by_allowlist_state`, `by_gate_eligibility`, and `by_budget_count`.
- Markdown report exits 0 and includes budget-counted, gate-eligible, allowlist/gate semantics, heatmap, high-spread, and prioritized cleanup sections.
- Hard-gate JSON may exit non-zero but stdout remains parseable JSON, `metadata.mode == "hard-gate"`, `hard_gate_failing_count` counts only gate-eligible findings, and `.entropy-baseline/latest.json` is absent or unchanged.
- `rg -- '--mode hard-gate' .github/workflows/governance.yml` exits with no matches.
- Docs explicitly state the report example is representative schema documentation, not a committed baseline or deletion queue.
- Docs explicitly state tracked retired active-tree returns are path findings, while governed historical/archive/OpenSpec text references stay text evidence.

## 5. Verification

- [x] 5.1 For #400, run `PYTHONDONTWRITEBYTECODE=1 uv run --no-sync python scripts/governance/audit_repo_entropy.py --format json` and record the counts/dispositions used by the triage artifact.
- [x] 5.2 For #400, run `openspec validate governance-5-e1-entropy-baseline-burndown --strict --no-interactive`.
- [x] 5.3 For #400, confirm `git diff --name-only` is limited to E1 OpenSpec clarification and governed docs/triage artifacts, with no runtime code, audit script, CI workflow, or node-27 frontend implementation changes.
- [x] 5.4 For #401, run `uv run --no-sync pytest -q tests/test_entropy_audit_script.py`.
- [x] 5.5 For #401, run `PYTHONDONTWRITEBYTECODE=1 uv run --no-sync python scripts/governance/audit_repo_entropy.py --format json`.
- [x] 5.6 For #401, run `PYTHONDONTWRITEBYTECODE=1 uv run --no-sync python scripts/governance/audit_repo_entropy.py --format markdown`.
- [x] 5.7 For #401, run `PYTHONDONTWRITEBYTECODE=1 uv run --no-sync python scripts/governance/audit_repo_entropy.py --mode hard-gate --format json` and confirm JSON remains parseable, hard-gate counts only gate-eligible findings, and `.entropy-baseline/latest.json` is not written.
- [x] 5.8 For #401, confirm `.github/workflows/governance.yml` does not use `--mode hard-gate`.
- [x] 5.9 For #402, run `uv run --no-sync pytest -q tests/test_entropy_audit_script.py`.
- [x] 5.10 For #402, run `PYTHONDONTWRITEBYTECODE=1 uv run --no-sync python scripts/governance/audit_repo_entropy.py --format json`.
- [x] 5.11 For #402, run `PYTHONDONTWRITEBYTECODE=1 uv run --no-sync python scripts/governance/audit_repo_entropy.py --format markdown`.
- [x] 5.12 For #402, run `PYTHONDONTWRITEBYTECODE=1 uv run --no-sync python scripts/governance/audit_repo_entropy.py --mode hard-gate --format json` and confirm JSON remains parseable, tracked retired-path findings have #401 normalized fields, and `.entropy-baseline/latest.json` is not written.
- [x] 5.13 Run `openspec validate governance-5-e1-entropy-baseline-burndown --strict --no-interactive`.
- [x] 5.14 For #403, run `PYTHONDONTWRITEBYTECODE=1 uv run --no-sync python scripts/governance/audit_repo_entropy.py --format json`.
- [x] 5.15 For #403, run `PYTHONDONTWRITEBYTECODE=1 uv run --no-sync python scripts/governance/audit_repo_entropy.py --format markdown`.
- [x] 5.16 For #403, run `PYTHONDONTWRITEBYTECODE=1 uv run --no-sync python scripts/governance/audit_repo_entropy.py --mode hard-gate --format json` and confirm JSON remains parseable and `.entropy-baseline/latest.json` is not written.
- [x] 5.17 For #403, run `rg -- '--mode hard-gate' .github/workflows/governance.yml` and confirm there are no matches.
- [x] 5.18 For #403, run `openspec validate governance-5-e1-entropy-baseline-burndown --strict --no-interactive`.
- [x] 5.19 For #403, confirm `git diff --name-only` is limited to `docs/governance/entropy-budget.md`, `docs/governance/entropy-report.example.md`, and E1 OpenSpec task/design evidence.
