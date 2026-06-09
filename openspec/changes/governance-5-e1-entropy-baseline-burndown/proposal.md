## Why

Governance-4 made repository entropy visible, but the current report still mixes actionable drift with historical evidence, archived inventory, and deliberate compatibility surfaces. The next governance stage needs a measurable burn-down contract before cleanup work can be judged by anything more reliable than raw deletion counts.

## What Changes

- Add a Governance-5 E1 change for report triage, normalized allowlist semantics, and budget-oriented cleanup evidence.
- Treat the Governance-4 report as a signal surface, not a deletion queue.
- Add finding-level semantics for allowlisted, gate-eligible, and budget-counted findings before any future hard-gate enablement.
- Add a tracked-path negative guard for retired active-tree paths so deleted placeholders cannot silently return.
- Keep CI report-only; do not enable `--mode hard-gate` as part of this change.

## Capabilities

### New Capabilities

- `entropy-baseline-burndown`: Provides machine-checkable entropy budget triage, normalized finding eligibility, retired-path return guards, and before/after cleanup evidence.

### Modified Capabilities

<!-- No existing product capability is modified. -->

## Impact

- Scripts: `scripts/governance/audit_repo_entropy.py`.
- Tests: `tests/test_entropy_audit_script.py` and any focused governance guard tests added for tracked retired paths.
- Docs: `docs/governance/entropy-budget.md`, `docs/governance/entropy-report.example.md`, and the governed inventory/status docs only as needed.
- CI: `.github/workflows/governance.yml` remains report-only and must not invoke hard-gate mode.
- Non-goals: deleting QHH diagnostic scripts, rewriting historical OpenSpec evidence to hide old tokens, or recreating Governance-2 cleanup issues.
