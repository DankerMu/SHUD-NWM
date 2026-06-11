## Context

The Governance-4 report currently exposes a stable baseline of entropy
findings, including stale display route tokens, placeholder path tokens, broad
e2e API mocks, and `apps.api.*` layer inversion imports. Several of those
findings are legitimate cleanup targets, but many are historical evidence,
archived inventory, or deliberate compatibility records.

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

### #401 normalized finding semantics slice

Fixture level: expanded
Repair intensity: high

Change surface:

- `scripts/governance/audit_repo_entropy.py` report schema, hard-gate metadata, Markdown rendering, and exit code behavior.
- `tests/test_entropy_audit_script.py` focused report schema and hard-gate tests.
- Minimal docs/example updates only if required to keep documented schema truthful.

Must preserve:

- Default `--mode report` remains report-only and exits 0 for known findings.
- No CI hard-gate enablement and no `.github/workflows/governance.yml` change.
- `.entropy-baseline/latest.json` is not created or updated by report or hard-gate runs.
- Existing `allowlist_reason` remains present for compatibility.
- Existing finding IDs, `check_id`, priority, role, module, and heatmap semantics remain stable unless explicitly extended.
- #402 tracked retired-path guard and #403 full report-example refresh remain future work.

Must add/change:

- Every finding exposes normalized allowlist semantics: `allowlist_state`, `allowlist_key`, `budget_counted`, and `gate_eligible`.
- Equivalent allowlist wording maps to a stable normalized allowlist key without losing the human-readable `allowlist_reason`.
- Hard-gate mode counts only finding records whose `gate_eligible` is true.
- Metadata exposes summary counts by `check_id`, priority, role, allowlist state, and gate eligibility.
- JSON remains parseable when explicit hard-gate mode exits non-zero.

Risk packs considered for #401:

- Public API / CLI / script entry: selected - report JSON/Markdown and CLI exit code are automation contracts.
- Config / project setup: selected - hard-gate metadata must not enable CI or write baselines.
- File IO / path safety / overwrite: selected - baseline path must remain read-only; no implicit `.entropy-baseline/latest.json` writes.
- Schema / columns / units / field names: selected - finding and metadata fields are schema contracts.
- Auth / permissions / secrets: not selected - no credential or permission surface.
- Concurrency / shared state / ordering: not selected - report construction is local and single-process.
- Resource limits / large input / discovery: selected - existing scan limits and skipped path behavior must be preserved.
- Legacy compatibility / examples: selected - archived/deterministic/delegated evidence must normalize without becoming active drift.
- Error handling / rollback / partial outputs: selected - hard-gate JSON must remain parseable even when exit code is non-zero.
- Release / packaging / dependency compatibility: not selected - no dependency or packaging change.
- Documentation / migration notes: selected - any minimal schema docs must match the new fields and preserve report-only policy.
- NHMS domain packs: not selected - no hydro-met, geospatial, SHUD, Slurm runtime, provider, manifest, or published artifact behavior changes.

Invariant Matrix:

- Governing invariant: the entropy report must separate human-readable evidence from machine gate/budget semantics without changing report-only CI posture or writing baselines.
- Source-of-truth identity/contract: each finding record's `check_id`, `allowlist_reason`, normalized `allowlist_key`, `allowlist_state`, `budget_counted`, and `gate_eligible` fields.
- Producers: `_collect_findings`, `FindingSpec`, `_finding_record`, allowlist normalization helpers, hard-gate metadata helpers.
- Validators/preflight: `tests/test_entropy_audit_script.py` schema, allowlist, summary, hard-gate, and baseline-write tests.
- Storage/cache/query: no persistent storage writes; `.entropy-baseline/latest.json` read state remains metadata only.
- Public routes/entrypoints: `scripts/governance/audit_repo_entropy.py --format json|markdown --mode report|hard-gate`.
- Frontend/downstream consumers: governance CI report-only job, docs/report examples, future #402/#403 consumers.
- Failure paths/rollback/stale state: hard-gate mode exits non-zero only for `gate_eligible` findings while stdout remains valid JSON/Markdown; report mode exits 0.
- Evidence/audit/readiness: focused pytest, JSON/Markdown commands, explicit hard-gate JSON command, baseline non-write check.
- Regression rows:
  - Deterministic mocked/preview/visual broad e2e finding -> `allowlist_state=allowlisted`, stable `allowlist_key`, `budget_counted=false`, `gate_eligible=false`.
  - Live-labeled broad e2e finding -> unallowlisted active drift, `budget_counted=true`, `gate_eligible=true`, hard-gate fails with parseable JSON.
  - Active stale display route token not in gated policy -> `budget_counted=true`, `gate_eligible=false`, hard-gate remains pass if no gate-eligible findings exist.
  - Existing OpenAPI delegated/fingerprint signals -> retain report-only/allowlisted semantics and do not become hard-gate failures.
  - Report and hard-gate runs with findings -> `.entropy-baseline/latest.json` is not written or modified.

Boundary-surface checklist:

- Shared helper roots: `FindingSpec`, `_finding_record`, `_metadata`, `_hard_gate_failing_count`, `_exit_code_for_report`, `render_markdown`.
- Public entrypoints: `main`, JSON stdout, Markdown stdout, process exit code.
- Read surfaces: repository scan helpers and existing allowlist classifiers.
- Write/delete/overwrite surfaces: none; baseline path must remain read-only metadata.
- Producer/consumer evidence boundaries: tests and docs must consume the same field names as emitted JSON.
- Unchanged downstream consumers: report-only CI workflow and existing heatmap/high-spread report consumers.

### #402 tracked retired-path guard slice

Fixture level: expanded
Repair intensity: high

Change surface:

- `scripts/governance/audit_repo_entropy.py` retired active-tree path guard.
- `tests/test_entropy_audit_script.py` focused temporary-repository tests.
- Minimal docs only if required to explain the guard.

Must preserve:

- Historical text references in docs, archived files, governance inventories, and completed OpenSpec evidence remain governed evidence, not active-tree reintroduction.
- Existing `placeholder-path-token` text-reference semantics remain compatible with #401 allowlist/budget fields.
- Default report mode remains report-only.
- No CI hard-gate enablement.
- No deletion of already retired paths.
- No baseline writes.

Must add/change:

- Add a tracked-file guard based on `git ls-files`, not `Path.exists()` alone.
- Detect tracked files under retired active-tree path prefixes: `apps/web`, hyphenated worker placeholders, `workers/sbatch_templates`, and `services/tile-publisher`.
- Emit a governance finding or equivalent guard result when a retired active-tree path returns as a tracked file, including force-added ignored files.
- Keep historical text references separate from tracked path reintroduction findings.

Risk packs considered for #402:

- Public API / CLI / script entry: selected - the audit report gains or refines a finding family through the existing CLI.
- Config / project setup: selected - behavior depends on git metadata and must work in temp repos as well as the main repo.
- File IO / path safety / overwrite: selected - the guard reads repository metadata and must not follow arbitrary filesystem state as truth.
- Schema / columns / units / field names: selected - new or refined finding records must preserve #401 fields and summaries.
- Auth / permissions / secrets: not selected - no credential surface.
- Concurrency / shared state / ordering: not selected - local report construction is single-process.
- Resource limits / large input / discovery: selected - tracked path detection should use bounded `git ls-files` output and scoped prefixes.
- Legacy compatibility / examples: selected - retired path text evidence must not be confused with tracked path return.
- Error handling / rollback / partial outputs: selected - missing or unavailable git metadata must fail report-only without crashing and without false tracked-path positives.
- Release / packaging / dependency compatibility: not selected - no dependencies or packaging changes.
- Documentation / migration notes: not selected - #402 should not change user-facing documentation unless implementation discovers an existing statement that would become false; any docs refresh remains #403.
- NHMS domain packs: not selected - no hydro-met, geospatial, SHUD, Slurm runtime, provider, manifest, or published artifact behavior changes.

Invariant Matrix:

- Governing invariant: only tracked files under retired active-tree prefixes count as returned retired paths; textual historical evidence remains governed evidence.
- Source-of-truth identity/contract: `git ls-files` tracked path strings matched against exact retired path prefixes, plus finding fields from #401.
- Producers: `_git_tracked_paths`, `_check_placeholder_paths` or a dedicated retired-path guard helper, `FindingSpec`, `_finding_record`.
- Validators/preflight: temporary git repository tests that add tracked retired path files and historical text references.
- Storage/cache/query: no writes; git index is read-only input.
- Public routes/entrypoints: `audit_repo_entropy.py --format json|markdown --mode report|hard-gate`.
- Frontend/downstream consumers: governance audit report, entropy budget docs, future #403 report example.
- Failure paths/rollback/stale state: missing git metadata or non-git temp roots do not crash the audit; no false positive for untracked filesystem-only retired paths.
- Evidence/audit/readiness: focused pytest, JSON/Markdown audit runs, OpenSpec validation, no baseline write.
- Regression rows:
  - Tracked `apps/web/README.md` -> retired path return finding with #401 normalized fields and budget/gate semantics.
  - Force-added ignored tracked retired path under a hyphenated worker prefix -> same finding behavior.
  - `docs/archived/**` or completed Governance-2 OpenSpec text mentioning retired paths -> no tracked-path-return finding.
  - Untracked filesystem-only retired path in temp repo -> no tracked-path-return finding from the tracked guard.
  - Non-git or unavailable-git metadata root -> audit completes report-only with no tracked-path-return false positives.
  - Normal active underscore worker/package paths -> no retired-path-return finding.

Non-goals for #402:

- No node-27/frontend old page retirement, route handoff, Playwright migration, or M15 visual lane changes.
- No docs/report example refresh beyond keeping the fixture truthful; #403 owns the report example and entropy-budget documentation.

Boundary-surface checklist:

- Shared helper roots: `_git_tracked_paths`, retired path prefix matcher, finding record normalization.
- Public entrypoints: JSON/Markdown report and hard-gate mode.
- Read surfaces: git index, repository text scan, temp repo fixtures.
- Write/delete/overwrite surfaces: none.
- Producer/consumer evidence boundaries: tests must distinguish path-return findings from placeholder text-token findings.
- Unchanged downstream consumers: #401 summary counts and report-only CI behavior.

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
