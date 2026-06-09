## Context

The audit generated a heatmap across six axes: structure, semantics, behavior, context, protocol, and control. The highest-risk rows were `services/orchestrator`, `packages/common`, `services/production_closure`, docs/OpenSpec, frontend e2e evidence, infra/CI, and tracked agent/artifact paths.

There is no `.entropy-baseline/latest.json` yet. Baseline writing must be an explicit maintainer decision because it creates project metadata and a future comparison point.

## Decisions

### D1. Start non-blocking

The first automation PR should report findings without failing CI. Hard gates should be introduced only after Governance-0 through Governance-3 have removed known existing failures.

### D2. Output machine-readable and human-readable reports

The script should emit JSON for automation and Markdown for maintainers. The Markdown should include a module heatmap and high-spread patterns.

Each finding should include `axis` or `axis_scores`, `governance_face`, `role`, `evidence_path`, `severity`, `priority`, `owner_area`, and optional allowlist reason. This makes reports actionable and issue-ready rather than a generic grep dump.

### D3. Model checks around four roles and four governance faces

The script should classify checks by:

- roles: `compute_control`, `display_readonly`, `slurm_gateway`, `shared_contract`
- governance face: role boundary, legacy/dead-code, docs alignment, entropy automation/control

### D4. Escalate only stable invariants to hard fail

Initial hard-fail candidates after cleanup:

- display env/compose contains compute-only env.
- production orchestrator references QHH diagnostic script tokens.
- live e2e specs use broad API mocks.
- standalone Slurm gateway includes business routes.
- generated OpenAPI/frontend type contract drift.
- paused CI jobs use hidden false conditions.
- command discipline regresses from `uv run` to system Python/ruff.
- tracked agent/artifact paths diverge from the documented ownership policy.

## Candidate Checks

- legacy/deprecated/placeholder/obsolete token inventory.
- `DIAGNOSTIC-ONLY` token inventory.
- `&& false` workflow check.
- `page.route('**/api/v1/**')` classification by mocked vs live e2e.
- stale `/hydro-met` and `HydroMetPage` references outside archived docs/tests.
- placeholder path inventory: `apps/web`, hyphenated workers, `workers/sbatch_templates`, `services/tile-publisher`.
- `.agents`, `.codex`, frontend artifacts tracked/ignored policy.
- Makefile/system Python vs `uv run`.
- `m22-placeholder` image tag context.
- hard-coded private host/path examples in docs/examples, classified as live receipt vs placeholder.
- layer inversion imports from `apps.api.auth` outside API layer.

## Risks / Mitigations

- **Risk: noisy false positives.** Mitigation: non-blocking first, allow explicit allowlist with reason and owner.
- **Risk: automation replaces judgment.** Mitigation: report states findings are governance signals, not automatic deletion instructions.
- **Risk: baseline churn.** Mitigation: do not write `.entropy-baseline/latest.json` without explicit confirmation.

## Verification

- `uv run python scripts/governance/audit_repo_entropy.py --format json`
- `uv run python scripts/governance/audit_repo_entropy.py --format markdown`
- Governance workflow dry run or local equivalent.

## Governance-4A Fixture

Issue #371 implements only the report script. Documentation, CI workflow, and
hard-gate wiring remain in later Governance-4 slices.

Fixture level: expanded
Project profile: NHMS
Repair intensity: medium
Change surface:
- New `scripts/governance/audit_repo_entropy.py`.
- Focused tests may be added for report schema, check classification, and
  no-baseline-write behavior.
Must preserve:
- Default report mode does not create or update `.entropy-baseline/latest.json`.
- Findings are report-only signals; script exit status stays successful for
  detected baseline findings unless a later hard-gate mode is explicitly added.
- Existing static guard tests remain the source of truth for enforced gates.
Must add/change:
- JSON and Markdown output formats.
- Six-axis heatmap fields: `structure`, `semantics`, `behavior`, `context`,
  `protocol`, `control`, and derived `priority`.
- Finding fields: `governance_face`, `role`, `evidence_path`, `severity`,
  `priority`, `owner_area`, and optional allowlist reason.
- Required check families from the #371 issue body in report-only mode.

Risk packs considered:
- Public API / CLI / script entry: selected - new script CLI and output modes.
- Config / project setup: selected - scans `.gitignore`, `.dockerignore`,
  workflows, Makefile, and ownership policy.
- File IO / path safety / overwrite: selected - recursively reads repository
  files and must avoid writing baseline files by default.
- Schema / columns / units / field names: selected - JSON report schema is a
  machine-readable contract for later CI/docs.
- Auth / permissions / secrets: selected - hard-coded private host/path and
  generated evidence checks must avoid leaking file contents unnecessarily.
- Concurrency / shared state / ordering: not selected - single-process read-only
  script with no shared runtime state.
- Resource limits / large input / discovery: selected - script must skip large
  generated/vendor directories and avoid walking ignored runtime data.
- Legacy compatibility / examples: selected - checks intentionally report
  placeholder/legacy paths and must not delete or rewrite them.
- Error handling / rollback / partial outputs: selected - CLI errors should be
  stable and report mode must not partially write baseline metadata.
- Release / packaging / dependency compatibility: selected - script should use
  standard-library or existing project dependencies so CI can run it with
  `uv run`.
- Documentation / migration notes: selected - output must be useful enough for
  later #372 docs and #373 non-blocking CI.
Domain packs:
- Published NHMS artifacts / display identity: selected - artifact ownership
  and frontend evidence paths are audited.
- Slurm production lifecycle / mock-vs-real parity: selected - standalone
  gateway route leakage and diagnostic-token checks cover Slurm boundaries.
- Run manifest / QC provenance: not selected - no manifest parsing behavior.
- Other NHMS domain packs: not selected - no geospatial, forcing, SHUD
  numerical, provider, or DB behavior change.
