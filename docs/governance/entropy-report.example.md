# Entropy Report Example

This page documents the representative JSON shape for
`governance-4a.entropy-report.v1`. It is schema documentation, not a committed
baseline, not a deletion queue, and not a source of current finding counts. Run
the live audit command when current evidence or counts matter.

Findings are governance signals. They tell the owner to inspect intent,
evidence, role boundary, and follow-up scope before changing code or docs.

## Generate Reports

```bash
uv run --no-sync python scripts/governance/audit_repo_entropy.py --format json
uv run --no-sync python scripts/governance/audit_repo_entropy.py --format markdown
uv run --no-sync python scripts/governance/audit_repo_entropy.py --mode hard-gate --format json
```

Report-only generation must not create or update
`.entropy-baseline/latest.json`. Explicit hard-gate generation also must not
create or update `.entropy-baseline/latest.json`. The Governance Audit workflow
remains report-only and must not pass `--mode hard-gate` unless a later
maintainer-approved enablement change does so explicitly.

`apps-api-layer-inversion` is included in `executed_check_families` so the
audit continues to detect non-API runtime imports from `apps.api.*`. After
cleanup in #418/#419, the live repository is expected to report zero findings
for that check. It is a future hard-gate candidate only after the zero baseline
is maintained by tests; it is not part of the current prepared gated check list
and the Governance Audit CI job remains report-only.

## Report-Only JSON Shape

```json
{
  "metadata": {
    "schema_version": "governance-4a.entropy-report.v1",
    "mode": "report-only",
    "generated_at": "2026-06-11T00:00:00+00:00",
    "repo_root": "/scratch/frd_muziyao/NWM",
    "baseline_path": ".entropy-baseline/latest.json",
    "baseline_exists": false,
    "baseline_written": false,
    "finding_count": 4,
    "check_family_count": 3,
    "budget_counted_count": 2,
    "gate_eligible_count": 1,
    "summary_counts": {
      "by_check_id": {
        "broad-e2e-api-mock": 2,
        "placeholder-path-exists": 1,
        "placeholder-path-token": 1
      },
      "by_priority": {
        "P2": 3,
        "P3": 1
      },
      "by_role": {
        "display_readonly": 2,
        "shared_contract": 2
      },
      "by_allowlist_state": {
        "allowlisted": 2,
        "unallowlisted": 2
      },
      "by_gate_eligibility": {
        "gate_eligible": 1,
        "not_gate_eligible": 3
      },
      "by_budget_count": {
        "budget_counted": 2,
        "not_budget_counted": 2
      }
    },
    "max_scanned_text_file_bytes": 1048576,
    "max_artifact_fingerprint_bytes": 1048576,
    "executed_check_families": [
      "role-env-boundary",
      "qhh-diagnostic-token",
      "paused-workflow-condition",
      "broad-e2e-api-mock",
      "stale-display-route-token",
      "placeholder-path-token",
      "placeholder-path-exists",
      "makefile-toolchain-discipline",
      "openapi-frontend-types-delegated",
      "openapi-frontend-types-presence",
      "openapi-frontend-types-signal",
      "slurm-gateway-route-leakage",
      "agent-artifact-ownership-policy",
      "agent-artifact-ignore-policy",
      "tracked-generated-artifact",
      "apps-api-layer-inversion"
    ],
    "skipped_path_families": [
      ".git",
      ".nhms-*",
      ".venv",
      "artifacts",
      "caches",
      "data",
      "dist",
      "node_modules"
    ]
  },
  "module_heatmap": [
    {
      "module": "apps/frontend",
      "structure": "low",
      "semantics": "low",
      "behavior": "medium",
      "context": "low",
      "protocol": "low",
      "control": "low",
      "priority": "P2",
      "finding_count": 2
    },
    {
      "module": "apps/web",
      "structure": "medium",
      "semantics": "low",
      "behavior": "low",
      "context": "low",
      "protocol": "low",
      "control": "low",
      "priority": "P2",
      "finding_count": 1
    },
    {
      "module": "openspec/governance-2-legacy-dead-code-retirement",
      "structure": "low",
      "semantics": "low",
      "behavior": "low",
      "context": "low",
      "protocol": "low",
      "control": "low",
      "priority": "P3",
      "finding_count": 1
    }
  ],
  "findings": [
    {
      "id": "ENT-0001",
      "check_id": "broad-e2e-api-mock",
      "title": "Frontend E2E path uses broad API mock",
      "axis": "behavior",
      "axis_scores": {
        "structure": "low",
        "semantics": "low",
        "behavior": "medium",
        "context": "low",
        "protocol": "low",
        "control": "low"
      },
      "governance_face": "docs alignment",
      "role": "display_readonly",
      "evidence_path": "apps/frontend/e2e/monitoring.spec.ts",
      "line": 168,
      "severity": "medium",
      "priority": "P2",
      "owner_area": "frontend e2e",
      "module": "apps/frontend",
      "allowlist_reason": null,
      "allowlist_key": null,
      "allowlist_state": "unallowlisted",
      "budget_counted": true,
      "gate_eligible": true,
      "description": "Broad API mocks can be mistaken for live display evidence.",
      "recommendation": "Keep broad API mocks in deterministic mocked regressions and label live evidence specs."
    },
    {
      "id": "ENT-0002",
      "check_id": "broad-e2e-api-mock",
      "title": "Deterministic frontend E2E path uses broad API mock",
      "axis": "behavior",
      "axis_scores": {
        "structure": "low",
        "semantics": "low",
        "behavior": "medium",
        "context": "low",
        "protocol": "low",
        "control": "low"
      },
      "governance_face": "docs alignment",
      "role": "display_readonly",
      "evidence_path": "apps/frontend/e2e/preview-deeplink.spec.ts",
      "line": 16,
      "severity": "medium",
      "priority": "P2",
      "owner_area": "frontend e2e",
      "module": "apps/frontend",
      "allowlist_reason": "deterministic mocked/preview/visual e2e broad mock",
      "allowlist_key": "broad-e2e-api-mock:deterministic-mocked-preview-visual",
      "allowlist_state": "allowlisted",
      "budget_counted": false,
      "gate_eligible": false,
      "description": "Broad API mocks can be mistaken for live display evidence.",
      "recommendation": "Keep broad API mocks in deterministic mocked regressions and label live evidence specs."
    },
    {
      "id": "ENT-0003",
      "check_id": "placeholder-path-exists",
      "title": "Tracked retired path returned to active tree",
      "axis": "structure",
      "axis_scores": {
        "structure": "medium",
        "semantics": "low",
        "behavior": "low",
        "context": "low",
        "protocol": "low",
        "control": "low"
      },
      "governance_face": "legacy/dead-code",
      "role": "shared_contract",
      "evidence_path": "apps/web/README.md",
      "line": null,
      "severity": "medium",
      "priority": "P2",
      "owner_area": "repo structure",
      "module": "apps/web",
      "allowlist_reason": null,
      "allowlist_key": null,
      "allowlist_state": "unallowlisted",
      "budget_counted": true,
      "gate_eligible": false,
      "description": "Tracked file returned under a retired active-tree prefix.",
      "recommendation": "Remove the tracked retired path or move the implementation to the canonical active underscore/package path."
    },
    {
      "id": "ENT-0004",
      "check_id": "placeholder-path-token",
      "title": "Placeholder or retired path token remains",
      "axis": "semantics",
      "axis_scores": {
        "structure": "low",
        "semantics": "low",
        "behavior": "low",
        "context": "low",
        "protocol": "low",
        "control": "low"
      },
      "governance_face": "legacy/dead-code",
      "role": "shared_contract",
      "evidence_path": "openspec/changes/governance-2-legacy-dead-code-retirement/tasks.md",
      "line": 22,
      "severity": "low",
      "priority": "P3",
      "owner_area": "docs/modules",
      "module": "openspec/governance-2-legacy-dead-code-retirement",
      "allowlist_reason": "governed completed OpenSpec evidence documents retired placeholder paths",
      "allowlist_key": "placeholder-path-token:governed-completed-openspec-retired-placeholder-evidence",
      "allowlist_state": "allowlisted",
      "budget_counted": false,
      "gate_eligible": false,
      "description": "Reference to a retired placeholder path remains in active scan scope.",
      "recommendation": "Use canonical underscore package paths or mark the reference as historical inventory with a narrow reason."
    }
  ],
  "high_spread_patterns": [
    {
      "pattern": "broad-e2e-api-mock",
      "occurrence_count": 2,
      "module_count": 1,
      "modules": [
        "apps/frontend"
      ],
      "roles": [
        "display_readonly"
      ],
      "governance_faces": [
        "docs alignment"
      ],
      "top_priority": "P2",
      "top_severity": "medium"
    }
  ]
}
```

## Hard-Gate Metadata

Hard-gate mode uses the same top-level shape. When invoked explicitly with
`--mode hard-gate`, metadata also includes:

```json
{
  "mode": "hard-gate",
  "hard_gate_status": "fail",
  "hard_gate_gated_check_ids": [
    "agent-artifact-ignore-policy",
    "agent-artifact-ownership-policy",
    "broad-e2e-api-mock",
    "makefile-toolchain-discipline",
    "openapi-frontend-types-presence",
    "paused-workflow-condition",
    "qhh-diagnostic-token",
    "role-env-boundary",
    "slurm-gateway-route-leakage",
    "tracked-generated-artifact"
  ],
  "hard_gate_failing_count": 1,
  "baseline_written": false
}
```

JSON output remains parseable when hard-gate mode exits non-zero.
`hard_gate_failing_count` counts finding records where `gate_eligible` is
`true`; it does not count every finding in a gated check family. Report-only
mode omits the `hard_gate_*` fields.

`apps-api-layer-inversion` is intentionally absent from the example
`hard_gate_gated_check_ids` list. A synthetic layer inversion remains an
unallowlisted, budget-counted role-boundary finding, but it is not
`gate_eligible` until a future maintainer-approved hard-gate enablement change.

## Field Notes

`metadata.schema_version` is the contract identifier for automation and docs
examples. Default report mode emits `metadata.mode == "report-only"` and exits
0 for known findings. `baseline_written` must remain `false` in report-only and
explicit hard-gate modes.

`metadata.finding_count` is the total number of emitted signals.
`metadata.budget_counted_count` counts unallowlisted active drift.
`metadata.gate_eligible_count` counts the subset of budget-counted findings that
explicit hard-gate mode would fail. `metadata.summary_counts` groups findings by
`by_check_id`, `by_priority`, `by_role`, `by_allowlist_state`,
`by_gate_eligibility`, and `by_budget_count`.

The scan limit fields are byte limits used by bounded readers and artifact
fingerprinting. `skipped_path_families` lists intentionally skipped repository
path families, including `.git`, virtualenv/dependency directories, large data
or artifact roots, cache roots, and `.nhms-*` work directories.

`module_heatmap` rows summarize the highest observed severity on each axis for
a module. The axis fields are `structure`, `semantics`, `behavior`, `context`,
`protocol`, and `control`; `priority` records the highest finding priority in
that module.

`findings` records are issue-ready signals. Required classification fields are
`governance_face`, `role`, `evidence_path`, `severity`, `priority`, and
`owner_area`. Normalized allowlist and budget fields are:

| Field | Meaning |
|---|---|
| `allowlist_reason` | Human-readable accepted-evidence reason; may be `null`. |
| `allowlist_key` | Stable normalized key derived from check ID and equivalent allowlist wording; `null` for unallowlisted findings. |
| `allowlist_state` | `allowlisted` when `allowlist_key` is present, otherwise `unallowlisted`. |
| `budget_counted` | `true` for unallowlisted active drift that consumes the cleanup budget. |
| `gate_eligible` | `true` only for budget-counted findings in the prepared hard-gate check set. |

Tracked retired active-tree files are path findings. A git-tracked file under a
configured retired web-app, hyphenated worker, sbatch-template, or
tile-publisher placeholder prefix is reported with `placeholder-path-exists`.
Governed historical/archive/OpenSpec text references stay text evidence and are
reported with `placeholder-path-token`; those records can be allowlisted when
they are intentionally retained for auditability.

`high_spread_patterns` groups repeated check families. Required fields are
`pattern`, `occurrence_count`, `module_count`, `modules`, `roles`,
`governance_faces`, `top_priority`, and `top_severity`.
