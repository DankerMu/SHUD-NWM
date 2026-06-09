# Entropy Report Example

This page shows the expected JSON report shape for
`governance-4a.entropy-report.v1`. The values are representative; use the live
audit command for current findings.

Findings are governance signals. They are not deletion instructions. A finding
means the owner should inspect intent, evidence, role boundary, and follow-up
scope before making a change.

## Generate Reports

```bash
uv run --no-sync python scripts/governance/audit_repo_entropy.py --format json
uv run --no-sync python scripts/governance/audit_repo_entropy.py --format markdown
uv run --no-sync python scripts/governance/audit_repo_entropy.py --mode hard-gate --format json
```

Report-only generation must not create or update
`.entropy-baseline/latest.json`. Explicit hard-gate generation also must not
create or update `.entropy-baseline/latest.json`.

## JSON Shape

```json
{
  "metadata": {
    "schema_version": "governance-4a.entropy-report.v1",
    "mode": "report-only",
    "generated_at": "2026-06-09T00:00:00+00:00",
    "repo_root": "/scratch/frd_muziyao/NWM",
    "baseline_path": ".entropy-baseline/latest.json",
    "baseline_exists": false,
    "baseline_written": false,
    "finding_count": 3,
    "check_family_count": 3,
    "budget_counted_count": 1,
    "gate_eligible_count": 1,
    "summary_counts": {
      "by_check_id": {
        "broad-e2e-api-mock": 1,
        "openapi-frontend-types-delegated": 1,
        "role-env-boundary": 1
      },
      "by_priority": {
        "P1": 1,
        "P2": 1,
        "P3": 1
      },
      "by_role": {
        "display_readonly": 2,
        "shared_contract": 1
      },
      "by_allowlist_state": {
        "allowlisted": 2,
        "unallowlisted": 1
      },
      "by_gate_eligibility": {
        "gate_eligible": 1,
        "not_gate_eligible": 2
      },
      "by_budget_count": {
        "budget_counted": 1,
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
      "module": "infra",
      "structure": "low",
      "semantics": "low",
      "behavior": "low",
      "context": "low",
      "protocol": "high",
      "control": "low",
      "priority": "P1",
      "finding_count": 1
    },
    {
      "module": "apps/frontend",
      "structure": "low",
      "semantics": "low",
      "behavior": "medium",
      "context": "low",
      "protocol": "low",
      "control": "low",
      "priority": "P2",
      "finding_count": 1
    }
  ],
  "findings": [
    {
      "id": "ENT-0001",
      "check_id": "role-env-boundary",
      "title": "Display configuration references compute-only environment",
      "axis": "protocol",
      "axis_scores": {
        "structure": "low",
        "semantics": "low",
        "behavior": "low",
        "context": "low",
        "protocol": "high",
        "control": "low"
      },
      "governance_face": "role boundary",
      "role": "display_readonly",
      "evidence_path": "infra/env/display.example",
      "line": 42,
      "severity": "high",
      "priority": "P1",
      "owner_area": "infra/runtime",
      "module": "infra",
      "allowlist_reason": null,
      "allowlist_key": null,
      "allowlist_state": "unallowlisted",
      "budget_counted": true,
      "gate_eligible": true,
      "description": "Display-facing env or compose file references a compute/control-plane boundary token.",
      "recommendation": "Keep display config limited to read-only runtime identity and public display inputs."
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
      "evidence_path": "apps/frontend/e2e/forecast.spec.ts",
      "line": 18,
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
      "check_id": "openapi-frontend-types-delegated",
      "title": "OpenAPI/frontend type drift delegated to existing contract checks",
      "axis": "protocol",
      "axis_scores": {
        "structure": "low",
        "semantics": "low",
        "behavior": "low",
        "context": "low",
        "protocol": "low",
        "control": "low"
      },
      "governance_face": "entropy automation/control",
      "role": "shared_contract",
      "evidence_path": "tests/test_openapi_drift.py",
      "line": null,
      "severity": "low",
      "priority": "P3",
      "owner_area": "api contract",
      "module": "openapi",
      "allowlist_reason": "existing OpenAPI drift tests are the enforced contract oracle",
      "allowlist_key": "openapi-frontend-types-delegated:existing-contract-oracle-delegation",
      "allowlist_state": "allowlisted",
      "budget_counted": false,
      "gate_eligible": false,
      "description": "Static OpenAPI and generated frontend types are present.",
      "recommendation": "Keep running the existing OpenAPI drift and frontend API type generation checks."
    }
  ],
  "high_spread_patterns": [
    {
      "pattern": "broad-e2e-api-mock",
      "occurrence_count": 8,
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

## Field Notes

`metadata.schema_version` is the contract identifier for automation and docs
examples. Default report mode emits `metadata.mode == "report-only"`. Explicit
`--mode hard-gate` emits `metadata.mode == "hard-gate"` plus
`hard_gate_status`, `hard_gate_gated_check_ids`, and
`hard_gate_failing_count`; JSON output remains parseable even when hard-gate
mode exits non-zero. `hard_gate_failing_count` counts finding records where
`gate_eligible` is `true`, not every finding from a check family.

`module_heatmap` rows summarize the highest observed severity on each axis for
a module. The axis fields are `structure`, `semantics`, `behavior`, `context`,
`protocol`, and `control`; `priority` records the highest finding priority in
that module.

`findings` records are issue-ready signals. Required classification fields are
`governance_face`, `role`, `evidence_path`, `severity`, `priority`, and
`owner_area`. `allowlist_reason` is present and may be `null`.
`allowlist_key` is the normalized machine identity for allowlisted findings and
is `null` for unallowlisted findings. `budget_counted` marks unallowlisted
active drift; `gate_eligible` marks findings counted by explicit hard-gate mode.

`high_spread_patterns` groups repeated check families. Required fields are
`pattern`, `occurrence_count`, `module_count`, `top_priority`, and `roles`;
the live schema also includes `modules`, `governance_faces`, and
`top_severity`.
