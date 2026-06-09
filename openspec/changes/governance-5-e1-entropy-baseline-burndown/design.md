## Context

The Governance-4 report currently exposes a stable baseline of entropy findings, including stale display route tokens, placeholder path tokens, broad e2e API mocks, and `apps.api.*` layer inversion imports. Several of those findings are legitimate cleanup targets, but many are historical evidence, archived inventory, or deliberate compatibility records.

Using total finding count or deletion line count as the only success metric would create two bad incentives: deleting evidence that should remain auditable, or adding allowlist text without reducing active drift. E1 defines the automation semantics needed to measure cleanup accurately.

## Goals / Non-Goals

**Goals:**

- Make each finding explicitly classifiable as actionable, historical, archived, false-positive, or deferred.
- Add machine-readable allowlist and gate-eligibility fields so future enforcement can distinguish live violations from accepted evidence.
- Add budget summaries that separate total findings from unallowlisted and gate-eligible findings.
- Prevent retired active-tree paths from returning through tracked files.
- Preserve report-only CI behavior while improving the report contract.

**Non-Goals:**

- No CI hard-gate enablement.
- No deletion of QHH diagnostic assets.
- No rewrite of historical OpenSpec records solely to remove search tokens.
- No frontend route, API contract, or layer-inversion implementation; those are separate Governance-5 changes.

## Issue Slice Fixtures

### #400 triage artifact slice

Fixture level: compact

Change surface:

- `docs/governance/**` triage artifact that records current entropy report counts and dispositions.
- Existing E1 OpenSpec files may be clarified for issue-level scope.

Must preserve:

- No runtime behavior changes.
- No changes to `scripts/governance/audit_repo_entropy.py`; schema and hard-gate semantics are owned by #401/#402.
- No CI hard-gate enablement.
- No QHH diagnostic deletion or relocation.
- No repeated Governance-2 placeholder deletion.
- No node-27 frontend route/page retirement.

Must add/change:

- A governed triage artifact that maps high-spread entropy finding families to `fix`, `historical`, `archived`, `false-positive`, or `defer`.
- The artifact records the report command/date, current counts, concrete owner epic/change for actionable families, and explicit non-goals.

Risk packs considered for #400:

- Public API / CLI / script entry: not selected - #400 runs the audit as evidence but does not change its CLI.
- Config / project setup: not selected - no workflow, environment, or build configuration changes.
- File IO / path safety / overwrite: not selected - no new file readers/writers beyond checked-in docs.
- Schema / columns / units / field names: not selected - no machine-readable report schema change; #401 owns schema semantics.
- Auth / permissions / secrets: not selected - no credential or permission surface.
- Concurrency / shared state / ordering: not selected - no shared runtime state.
- Resource limits / large input / discovery: not selected - no audit discovery logic change.
- Legacy compatibility / examples: selected - historical, archived, diagnostic, and compatibility findings must not be mistaken for active drift.
- Error handling / rollback / partial outputs: not selected - no runtime failure path changes.
- Release / packaging / dependency compatibility: not selected - no package or dependency changes.
- Documentation / migration notes: selected - the artifact is a current governance document consumed by later Governance-5 issues.
- NHMS domain packs: not selected - no geospatial, forcing, SHUD runtime, PostGIS/Timescale, Slurm lifecycle, provider, run-manifest, or published artifact behavior changes.

Required #400 evidence:

- Run `PYTHONDONTWRITEBYTECODE=1 uv run --no-sync python scripts/governance/audit_repo_entropy.py --format json` and record the generated report counts in the triage artifact.
- Run `openspec validate governance-5-e1-entropy-baseline-burndown --strict --no-interactive`.
- Confirm `git diff --name-only` contains no runtime code, audit script, CI workflow, or node-27 frontend implementation changes.

Non-goals for #400:

- Normalized allowlist fields, finding-level gate eligibility, and summary schema changes are #401.
- Tracked retired-path return guard implementation is #402.
- Entropy budget/report example schema refresh is #403.
- Display route cleanup, API retirement, and layer-inversion cleanup belong to E2/E3/E4.

## Decisions

### D1. Use finding-level semantics instead of check-family gates

Hard-gate behavior must be based on individual finding eligibility, not only `check_id`. For example, a broad mock in a live display spec is a gate candidate, while a broad mock in an explicitly historical visual regression lane may be budgeted differently.

### D2. Treat archived evidence as governed evidence, not active drift

Paths such as `docs/archived/**`, governed legacy inventories, and completed OpenSpec changes may contain old path names by design. The audit should classify those as historical or archived where appropriate, while still detecting active current docs that present retired paths as current entrypoints.

### D3. Add tracked retired-path guards

`Path.exists()` is not sufficient protection because a retired path can return as a tracked file or through a force-add. The governance guard should check `git ls-files` for retired active-tree paths such as `apps/web`, hyphenated worker placeholders, `workers/sbatch_templates`, and `services/tile-publisher`.

### D4. Keep baseline writes explicit

The report may support before/after budget comparison, but it must not write `.entropy-baseline/latest.json` unless a maintainer explicitly approves a future baseline write change.

## Risks / Trade-offs

- **Risk: cleanup becomes allowlist-only.** Mitigation: require before/after budget evidence and explain every new allowlist key.
- **Risk: historical evidence is hidden.** Mitigation: classify historical evidence instead of deleting or rewriting it.
- **Risk: future hard-gate becomes noisy.** Mitigation: add `gate_eligible` semantics before any CI enforcement.
- **Risk: report schema churn breaks CI consumers.** Mitigation: update schema tests and report examples in the same change.

## Migration Plan

1. Add normalized finding semantics and summary fields while preserving existing report shape where possible.
2. Add tests for report schema, hard-gate eligibility, and retired path guards.
3. Update entropy budget docs and report example.
4. Re-run report-only governance audit and confirm no baseline write.
5. Leave `.github/workflows/governance.yml` in report-only mode.

## Open Questions

- Whether `.entropy-baseline/latest.json` should be introduced in a later Governance-6 stage after this burn-down has stabilized.
