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

## Governance-4B Fixture

Issue #372 documents the report contract and entropy budget only. It does not
change the audit script, add CI jobs, or enable hard-gate behavior.

Fixture level: compact
Project profile: NHMS
Repair intensity: low
Change surface:
- `docs/governance/entropy-budget.md`
- `docs/governance/entropy-report.example.md`
Must preserve:
- The audit script remains report-only by default.
- `.entropy-baseline/latest.json` remains unwritten unless a maintainer
  explicitly confirms baseline creation in a future change.
- Governance-4C owns non-blocking CI and Governance-4D owns hard-gate wiring.
Must add/change:
- A maintained entropy budget describing report-only, non-blocking CI, and
  future hard-gate stages.
- A report example matching `governance-4a.entropy-report.v1` top-level fields:
  `metadata`, `module_heatmap`, `findings`, and `high_spread_patterns`.
- Documentation of the four roles and four governance faces used by the audit
  report.

Risk packs considered:
- Public API / CLI / script entry: not selected - docs-only change; script CLI
  is unchanged.
- Config / project setup: selected - future CI and baseline policy are
  documented but not implemented.
- File IO / path safety / overwrite: selected - baseline write policy must
  forbid accidental `.entropy-baseline/latest.json` creation.
- Schema / columns / units / field names: selected - example report fields must
  match the script schema.
- Auth / permissions / secrets: not selected - docs must not add credentials or
  private deployment paths.
- Concurrency / shared state / ordering: not selected - docs-only change.
- Resource limits / large input / discovery: not selected - scan limits are
  described from existing metadata only.
- Legacy compatibility / examples: selected - docs distinguish report findings
  from deletion instructions.
- Error handling / rollback / partial outputs: not selected - no runtime error
  path changes.
- Release / packaging / dependency compatibility: not selected - no dependency
  or packaging change.
- Documentation / migration notes: selected - primary scope.

Required evidence:
- `uv run --no-sync python scripts/governance/audit_repo_entropy.py --format json`
  parses and exposes the fields used by the example.
- Extract the fenced JSON example from
  `docs/governance/entropy-report.example.md` and compare its schema against
  live JSON output from the audit script. The comparison must cover top-level
  keys, `metadata.schema_version`, heatmap axes, finding fields, and
  high-spread pattern fields.
- `uv run --no-sync python scripts/governance/audit_repo_entropy.py --format markdown`
  still emits heatmap and cleanup-target sections.
- Markdown lint or equivalent local docs check covers the two new governance
  docs and this OpenSpec change.
- `openspec validate governance-4-entropy-automation --strict --no-interactive`
  passes.

Non-goals:
- No changes to `scripts/governance/audit_repo_entropy.py`.
- No `.github/workflows/**` changes.
- No `.entropy-baseline/latest.json` creation or update.

## Governance-4C Fixture

Issue #373 integrates the existing report-only audit into CI. It must make the
report visible in pull requests and master pushes without converting existing
known findings into failures.

Fixture level: expanded
Project profile: NHMS
Repair intensity: medium
Change surface:
- `.github/workflows/governance.yml` or a narrowly scoped addition to an
  existing workflow.
- OpenSpec task evidence for section 3.
Must preserve:
- The audit script remains report-only and exits successfully for baseline
  findings.
- `.entropy-baseline/latest.json` is not created or updated.
- Governance-4D owns disabled hard-gate preparation; this change must not add
  hard-gate CLI flags, fail thresholds, or required status semantics for
  known findings.
Must add/change:
- CI runs JSON and Markdown entropy reports.
- CI exposes reports by upload artifact, job summary, log output, or an
  equivalent reviewable surface.
- CI remains non-blocking for known report findings; only workflow/tooling
  failures such as script exceptions, artifact upload failures, or invalid
  command syntax should fail the job.
- Report paths are fixed for this slice:
  `artifacts/governance/entropy-report.json` and
  `artifacts/governance/entropy-report.md`.

Risk packs considered:
- Public API / CLI / script entry: selected - CI invokes the audit script CLI.
- Config / project setup: selected - workflow behavior and triggers change.
- File IO / path safety / overwrite: selected - workflow writes temporary
  report files and must not write `.entropy-baseline/latest.json`.
- Schema / columns / units / field names: selected - CI must preserve JSON and
  Markdown report artifacts from the existing schema.
- Auth / permissions / secrets: selected - workflow should use read-only repo
  permissions and avoid exposing secrets.
- Concurrency / shared state / ordering: selected - workflow should use a
  normal concurrency key or remain independent of stateful gates.
- Resource limits / large input / discovery: selected - audit scan must run
  within a bounded CI timeout.
- Legacy compatibility / examples: selected - existing findings remain report
  signals, not failures.
- Error handling / rollback / partial outputs: selected - report generation
  failures should be visible, and artifact upload should run when reports
  exist.
- Release / packaging / dependency compatibility: selected - CI must install or
  use the repository Python/uv toolchain predictably.
- Documentation / migration notes: selected - PR evidence must state
  non-blocking behavior and future hard-gate boundary.
- New audit checks / hard-gate enforcement: not selected - #373 only wires the
  existing report-only command into CI; #374 owns fail-on-finding behavior.
- Baseline writes: not selected - #373 must not add any command or flag that
  creates or updates `.entropy-baseline/latest.json`.
Domain packs:
- Published NHMS artifacts / display identity: not selected - no published
  artifact, display API, or frontend evidence behavior changes.
- Slurm production lifecycle / mock-vs-real parity: not selected - no Slurm
  gateway, scheduler, retry, cancellation, or live/mock parity behavior changes.
- Run manifest / QC provenance: not selected - no manifest parsing or QC
  provenance changes.
- Other NHMS domain packs: not selected - no geospatial, forcing, SHUD
  numerical, provider, DB, or pipeline runtime behavior change.

Required evidence:
- Local equivalent of the workflow command writes JSON and Markdown reports to
  `artifacts/governance/entropy-report.json` and
  `artifacts/governance/entropy-report.md`, then `test -s` verifies both files
  exist.
- Parse the generated JSON and assert `metadata.mode == "report-only"` and
  `metadata.baseline_written == false`.
- Verify the artifact upload or `$GITHUB_STEP_SUMMARY` step references the same
  fixed JSON and Markdown report paths produced by the workflow command.
- CI workflow syntax is valid by inspection or a local parser/action linter if
  available.
- `.entropy-baseline/latest.json` remains absent before and after report runs.
- `openspec validate governance-4-entropy-automation --strict --no-interactive`
  passes.

Non-goals:
- No new audit check families.
- No hard-gate mode or fail-on-finding threshold.
- No baseline file creation or update.
