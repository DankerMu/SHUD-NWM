## Why

Manual governance will decay unless the repository can detect entropy regressions automatically. The audit identified high-spread patterns: role-boundary drift, legacy tokens, mocked-live e2e ambiguity, paused CI jobs, command discipline drift, stale docs, generated artifact ownership ambiguity, and structural layer inversions.

## What Changes

- Add a non-blocking entropy audit script and report format.
- Add a governance workflow that initially uploads or prints reports without failing PRs.
- Define escalation rules for converting selected checks into hard gates after the baseline stabilizes.
- Optionally create `.entropy-baseline/latest.json` only after explicit maintainer confirmation.

## Capabilities

### New Capabilities

- `entropy-automation`: Provides repeatable repository entropy scanning, reporting, and staged CI enforcement.

### Modified Capabilities

<!-- No existing product capability is modified. -->

## Impact

- Dependency: starts after `governance-0-ci-contract-baseline` is merged and after Governance-1/2/3 have either landed or supplied explicit baseline findings to the report allowlist.
- Scripts: `scripts/governance/audit_repo_entropy.py`.
- Docs: `docs/governance/entropy-budget.md`, `docs/governance/entropy-report.example.md`.
- CI: `.github/workflows/governance.yml` or additions to existing CI.
- Optional baseline: `.entropy-baseline/latest.json` only with explicit user/maintainer approval.
- Check targets: role/env boundaries, diagnostic tokens, `&& false`, e2e mocks, `/hydro-met` stale route references, placeholder paths, toolchain discipline, agent/artifact ownership.
