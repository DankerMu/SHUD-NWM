# Entropy Budget

This page defines how Governance-4 entropy findings are interpreted. The
current audit is a governance signal surface, not a deletion queue and not a CI
gate.

## Authority

Source-of-truth order:

1. `scripts/governance/audit_repo_entropy.py` defines the current report schema
   and scan behavior.
2. `docs/governance/ROLE_BOUNDARY.md` defines the role vocabulary.
3. `docs/governance/LEGACY_DEAD_CODE_INVENTORY.md` defines governed
   legacy/dead-code status and active counterparts.
4. `docs/governance/DOC_STATUS.md` defines document authority and tracked
   agent/artifact ownership.
5. The active OpenSpec change
   `openspec/changes/governance-4-entropy-automation` defines rollout scope.

When this page disagrees with the script schema, role boundary inventory,
legacy/dead-code inventory, document status authority, or active OpenSpec
tasks, those sources win.

## Roles

The audit classifies each finding under one role. The role explains which
boundary owns the cleanup decision; it does not imply the file should be moved
or deleted.

| Role | Meaning |
|---|---|
| `compute_control` | Compute-side control plane, scheduler/orchestrator paths, writable runtime roots, production closure, and worker execution. |
| `display_readonly` | Display API/frontend behavior, readonly deployment posture, display evidence, and display-only environment boundaries. |
| `slurm_gateway` | Standalone Slurm gateway surface limited to health and `/api/v1/slurm/*` behavior. |
| `shared_contract` | Shared schemas, API contracts, generated types, migrations, governed docs/status policy, and repository-wide automation contracts. |

`shared_contract` is a governance category, not a runtime `ServiceRole` value.

## Governance Faces

The audit also assigns each finding to one governance face. The face explains
what kind of drift the finding represents.

| Governance face | Meaning |
|---|---|
| `role boundary` | A role may be exposing, importing, configuring, or depending on capability that belongs to another role. |
| `legacy/dead-code` | A placeholder, retired path, diagnostic token, or archived-looking surface still appears in active scan scope. |
| `docs alignment` | A doc, test, or evidence path can be mistaken for current runtime or live validation truth. |
| `entropy automation/control` | Automation, generated contracts, toolchain discipline, CI posture, or tracked artifact ownership needs clearer control. |

Findings are governance signals. They identify where maintainers should inspect
intent, ownership, and evidence. They are not automatic instructions to delete
files, remove tests, change routes, or rewrite docs.

## Stage Budget

The entropy budget is staged so the project can make current drift visible
before converting stable invariants into enforcement.

| Stage | Owner issue | Budget | Allowed action | Not allowed in this stage |
|---|---|---|---|---|
| Report-only local audit | Governance-4A/#371 and Governance-4B/#372 | Existing findings may be present. The budget is visibility and schema stability, not pass/fail cleanup. | Run JSON/Markdown reports locally, document the schema, and use findings to open targeted follow-up work. | No CI job, no failing gate, no baseline write, no scripted cleanup. |
| Non-blocking CI report | Governance-4C/#373 | CI may publish findings without failing PRs for known baseline state. | Add a workflow/job that emits or uploads the report and records known findings for review. | No hard fail for known baseline findings; no silent `.entropy-baseline/latest.json` creation. |
| Future hard gate | Governance-4D/#374 | Only stable, low-noise invariants may become fail conditions after cleanup or explicit waiver. | Add CLI/config support for hard-gate checks and fail on selected regressions. | No broad hard gate before Governance-4C evidence and maintainer-approved invariants. |

Governance-4C and Governance-4D remain future work. This page documents their
intended boundaries but does not implement CI or hard-gate behavior.

## Baseline Write Policy

`.entropy-baseline/latest.json` is project metadata. It creates a comparison
point for future trend analysis, so it must not be created or updated as an
incidental side effect of report generation.

Policy:

- `uv run --no-sync python scripts/governance/audit_repo_entropy.py --format json`
  must not create or update `.entropy-baseline/latest.json`.
- `uv run --no-sync python scripts/governance/audit_repo_entropy.py --format markdown`
  must not create or update `.entropy-baseline/latest.json`.
- A future baseline write requires explicit maintainer confirmation in the
  issue or PR that creates or updates the baseline.
- Governance-4B/#372 must not create `.entropy-baseline/latest.json`.

The current report metadata may include `baseline_exists` and
`baseline_written`. In report-only mode, `baseline_written` must remain `false`.

## Hard-Gate Candidates

After non-blocking CI has produced enough evidence, Governance-4D may promote
selected stable invariants to hard gates. Candidate gates include:

- display env/compose files containing compute-only environment values.
- production scheduler/orchestrator paths referencing QHH diagnostic script
  tokens.
- live display e2e specs using broad `page.route('**/api/v1/**')` mocks.
- standalone Slurm gateway apps exposing business, static, frontend, forecast,
  model, or pipeline routes.
- OpenAPI and generated frontend type drift, either directly or through the
  existing contract-drift oracle.
- workflow jobs hidden behind `&& false`.
- Makefile commands bypassing the repository-managed `uv run` Python toolchain.
- tracked `.agents`, `.codex`, or frontend artifact paths conflicting with
  `docs/governance/DOC_STATUS.md`.

These are candidates, not active hard gates in Governance-4B.
