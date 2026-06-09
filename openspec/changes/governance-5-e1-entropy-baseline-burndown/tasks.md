## Issue Ownership

#400 owns only task group 1 and the #400 evidence rows in task group 5. It is a
compact documentation/governance slice and must not change audit script logic,
runtime code, CI workflows, or node-27 frontend implementation files.

#401 owns task group 2, #402 owns task group 3, and #403 owns task group 4 after
#401/#402 land.

## 1. Baseline Triage Contract

- [ ] 1.1 Capture the current Governance-4 report counts and classify each high-spread family as `fix`, `historical`, `archived`, `false-positive`, or `defer`.
- [ ] 1.2 Document non-goals: no QHH diagnostic deletion, no repeated Governance-2 placeholder deletion, no CI hard-gate enablement.
- [ ] 1.3 Add or update a governed triage artifact that maps the report families to downstream Governance-5 changes.

## 2. Audit Schema Semantics

- [ ] 2.1 Add normalized finding fields for allowlist state, allowlist key, budget countability, and gate eligibility.
- [ ] 2.2 Update hard-gate evaluation to count only gate-eligible findings instead of whole check families.
- [ ] 2.3 Add summary counts by `check_id`, priority, role, allowlist state, and gate eligibility.
- [ ] 2.4 Preserve report-only behavior and verify `.entropy-baseline/latest.json` is not created or updated.

## 3. Retired Path Return Guard

- [ ] 3.1 Add a tracked-file guard for retired active-tree paths: `apps/web`, hyphenated worker placeholders, `workers/sbatch_templates`, and `services/tile-publisher`.
- [ ] 3.2 Distinguish tracked retired path reintroduction from historical text references in archived docs or governance inventories.
- [ ] 3.3 Add focused tests proving a temporary reintroduced retired path is reported or fails the guard.

## 4. Documentation And Report Example

- [ ] 4.1 Update `docs/governance/entropy-budget.md` with E1 burn-down semantics and the continued report-only CI policy.
- [ ] 4.2 Update `docs/governance/entropy-report.example.md` to include normalized allowlist, budget, and gate eligibility fields.
- [ ] 4.3 Verify the example schema against live JSON output.

## 5. Verification

- [ ] 5.1 For #400, run `PYTHONDONTWRITEBYTECODE=1 uv run --no-sync python scripts/governance/audit_repo_entropy.py --format json` and record the counts/dispositions used by the triage artifact.
- [ ] 5.2 For #400, run `openspec validate governance-5-e1-entropy-baseline-burndown --strict --no-interactive`.
- [ ] 5.3 For #400, confirm `git diff --name-only` is limited to E1 OpenSpec clarification and governed docs/triage artifacts, with no runtime code, audit script, CI workflow, or node-27 frontend implementation changes.
- [ ] 5.4 For #401/#402/#403, run `uv run --no-sync pytest -q tests/test_entropy_audit_script.py`.
- [ ] 5.5 For #401/#402/#403, run `PYTHONDONTWRITEBYTECODE=1 uv run --no-sync python scripts/governance/audit_repo_entropy.py --format json`.
- [ ] 5.6 For #401/#402/#403, run `PYTHONDONTWRITEBYTECODE=1 uv run --no-sync python scripts/governance/audit_repo_entropy.py --format markdown`.
- [ ] 5.7 For #401/#402/#403, run `PYTHONDONTWRITEBYTECODE=1 uv run --no-sync python scripts/governance/audit_repo_entropy.py --mode hard-gate --format json` and confirm JSON remains parseable, hard-gate counts only gate-eligible findings, and `.entropy-baseline/latest.json` is not written.
- [ ] 5.8 For #401/#402/#403, confirm `.github/workflows/governance.yml` does not use `--mode hard-gate`.
- [ ] 5.9 Run `openspec validate governance-5-e1-entropy-baseline-burndown --strict --no-interactive`.
