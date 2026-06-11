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
5. The active OpenSpec changes
   `openspec/changes/governance-4-entropy-automation` and
   `openspec/changes/governance-5-e1-entropy-baseline-burndown` define rollout
   scope.

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
| Disabled hard gate | Governance-4D/#374 | Only the prepared, explicit invariant list is eligible for fail conditions, and only when a maintainer invokes hard-gate mode. | Run `--mode hard-gate` locally or in temporary fixtures to prove future enforcement semantics. | No CI hard-gate enablement; no broad fail-on-finding behavior; no baseline write. |
| Baseline burn-down semantics | Governance-5 E1/#400-#403 | Findings are split into total findings, budget-counted findings, and gate-eligible findings. Historical, archived, delegated, and false-positive evidence can stay visible without consuming the cleanup budget. | Use normalized finding fields and summary counters to measure cleanup, document owner issues, and guard retired active-tree paths from returning. | No audit logic changes in #403; no CI hard-gate enablement; no baseline write; no example treated as a committed baseline. |

Governance-4C is active as a non-blocking report workflow. Governance-4D
prepares an explicit hard-gate mode, but CI remains report-only until a later
maintainer-approved enablement change.

Governance-5 E1 triage is tracked in
`docs/governance/entropy-burndown-triage.md`. That artifact records the current
report counts, high-spread family dispositions, owner issues/changes, and
the #400-specific non-goals before later automation work changes the report
semantics.

## Finding Semantics

Each JSON finding carries both human-readable evidence and machine-readable
budget fields:

| Field | Meaning |
|---|---|
| `allowlist_reason` | Human-readable explanation for accepted evidence. It remains present for compatibility and may be `null`. |
| `allowlist_key` | Stable normalized key derived from the check ID and equivalent allowlist wording. It is `null` when the finding is not allowlisted. |
| `allowlist_state` | `allowlisted` when `allowlist_key` is present; otherwise `unallowlisted`. |
| `budget_counted` | `true` for unallowlisted active drift that consumes the Governance-5 cleanup budget. Allowlisted historical, archived, delegated, false-positive, or report-only evidence is `false`. |
| `gate_eligible` | `true` only when the finding is budget-counted and its `check_id` belongs to the prepared hard-gate check set. Hard-gate mode counts this field at finding level. |

`allowlist_key` is the automation identity. `allowlist_reason` is explanatory
text for maintainers and reports. Equivalent wording for the same accepted
evidence should normalize to the same key instead of creating separate budget
categories.

## Summary Counters

The report separates three counts that must not be used interchangeably:

| Count | Source | Meaning |
|---|---|---|
| Total findings | `metadata.finding_count` | Every emitted governance signal, including active drift, historical evidence, archived records, delegated checks, and false positives. This is useful for scan coverage, not for burn-down success. |
| Budget-counted findings | `metadata.budget_counted_count` and `summary_counts.by_budget_count.budget_counted` | Unallowlisted active drift that consumes the cleanup budget. These findings should map to owner issues or an explicit later disposition. |
| Gate-eligible findings | `metadata.gate_eligible_count` and `summary_counts.by_gate_eligibility.gate_eligible` | Budget-counted findings that explicit hard-gate mode would count as failures. This is a subset of budget-counted findings. |

`metadata.summary_counts` also groups findings by `check_id`, priority, role,
allowlist state, gate eligibility, and budget count. Those counters are for
reporting and trend review. They do not enable CI failure by themselves.

## Retired Active-Tree Paths

Governance-5 E1 distinguishes tracked path reintroduction from text evidence:

- A tracked file under a retired active-tree prefix is a path finding. The audit
  checks git-tracked path identity for configured retired web-app, hyphenated
  worker, sbatch-template, and tile-publisher placeholder prefixes. If such a
  file returns to the active tree, the report emits a
  `placeholder-path-exists` finding.
- Historical, archive, inventory, and completed OpenSpec text that mentions a
  retired path remains text evidence. These references are reported through
  `placeholder-path-token` semantics and may be allowlisted with a governed
  reason when they are retained for auditability.
- Untracked filesystem-only files are not path reintroduction evidence for this
  guard. The source of truth is `git ls-files`, including force-added ignored
  files.

This distinction prevents active retired paths from silently returning while
preserving governed historical/archive/OpenSpec evidence.

## Baseline Write Policy

`.entropy-baseline/latest.json` is project metadata. It creates a comparison
point for future trend analysis, so it must not be created or updated as an
incidental side effect of report generation.

Policy:

- `uv run --no-sync python scripts/governance/audit_repo_entropy.py --format json`
  must not create or update `.entropy-baseline/latest.json`.
- `uv run --no-sync python scripts/governance/audit_repo_entropy.py --format markdown`
  must not create or update `.entropy-baseline/latest.json`.
- `uv run --no-sync python scripts/governance/audit_repo_entropy.py --mode hard-gate --format json`
  must not create or update `.entropy-baseline/latest.json`.
- A future baseline write requires explicit maintainer confirmation in the
  issue or PR that creates or updates the baseline.
- Governance-4B/#372 must not create `.entropy-baseline/latest.json`.

The current report metadata may include `baseline_exists` and
`baseline_written`. In report-only and explicit hard-gate modes,
`baseline_written` must remain `false`.

## Hard-Gate Candidates

Governance-4D prepares disabled-by-default hard-gate evaluation for selected
stable invariants. The CLI surface is explicit:

```bash
uv run --no-sync python scripts/governance/audit_repo_entropy.py --mode hard-gate --format json
uv run --no-sync python scripts/governance/audit_repo_entropy.py --mode hard-gate --format markdown
```

The default remains `--mode report`, which emits
`metadata.mode == "report-only"` and exits 0 for known findings. Explicit
hard-gate mode emits `metadata.mode == "hard-gate"`,
`metadata.hard_gate_status`, `metadata.hard_gate_gated_check_ids`, and
`metadata.hard_gate_failing_count`; it exits non-zero only when finding records
are marked `gate_eligible: true`. JSON remains parseable even on a hard-gate
failure.

Prepared gated check IDs:

- `role-env-boundary`
- `qhh-diagnostic-token`
- `broad-e2e-api-mock`
- `slurm-gateway-route-leakage`
- `openapi-frontend-types-presence`
- `paused-workflow-condition`
- `makefile-toolchain-discipline`
- `agent-artifact-ownership-policy`
- `agent-artifact-ignore-policy`
- `tracked-generated-artifact`

The prepared check list is only an eligibility boundary. Allowlisted,
historical, archived, false-positive, delegated, or report-only accepted
evidence remains `gate_eligible: false` even when its `check_id` appears in the
list. Unallowlisted active drift is `budget_counted: true`; hard-gate mode
counts only the budget-counted findings whose individual policy also marks them
gate-eligible.

`openapi-frontend-types-delegated` and `openapi-frontend-types-signal` remain
report-only signals. The Governance Audit workflow must not pass
`--mode hard-gate` until a later enablement change explicitly makes the gate a
required CI status.
