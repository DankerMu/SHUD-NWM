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
