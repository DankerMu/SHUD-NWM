# Archive Status Materialization Ledger

Status date: 2026-06-24

This ledger records the first governed archive-marker materialization set for
issue #681. It preserves historical evidence text and classifies stale-looking
route/path evidence through complete archive status markers instead of deleting
or rewriting archived material.

## Evidence Summary

- Before report command:
  `uv run python scripts/governance/audit_repo_entropy.py --format json`.
- Before archive route/path budget total:
  `453` budget-counted findings under `docs/archived/**` and
  `openspec/changes/archive/**` for
  `placeholder-path-token` or `stale-display-route-token`.
- Before selected target budget total:
  `115` budget-counted findings:
  `44` `placeholder-path-token` and `71` `stale-display-route-token`.
- After report:
  `/tmp/entropy-681-after.json`.
- After archive route/path budget total:
  `338` budget-counted findings:
  `38` `placeholder-path-token` and `300` `stale-display-route-token`.
- After selected target budget total:
  `0` budget-counted findings.
- After selected complete-marker allowlist count:
  `152` findings. This count includes all selected route/path findings now
  covered by whole-document complete markers, not only the before
  budget-counted subset.
- Baseline write status:
  `baseline_written=false`.

## Authority Keys

| Key | current_authority | superseded_by |
|---|---|---|
| `slurm-current` | `services/slurm_gateway/config.py`; `infra/sbatch/**`; `docs/runbooks/two-node-deployment-overview.md` role/topology context | `services/slurm_gateway/config.py`; `infra/sbatch/**` |
| `gov2-current` | `docs/governance/LEGACY_DEAD_CODE_INVENTORY.md`; `openspec/specs/legacy-dead-code-retirement/spec.md`; `docs/governance/DOC_STATUS.md` | `openspec/specs/legacy-dead-code-retirement/spec.md` |
| `m26-current` | `openspec/specs/single-map-shell-routing/spec.md`; `openspec/specs/legacy-display-page-retirement/spec.md`; `openspec/specs/inplace-overview-basin-detail/spec.md`; `openspec/specs/map-feature-popups/spec.md`; `openspec/specs/met-station-cluster-layer/spec.md`; `docs/runbooks/display-readonly-live-mvt.md`; `docs/runbooks/two-node-deployment-overview.md` | `openspec/specs/single-map-shell-routing/spec.md`; `openspec/specs/legacy-display-page-retirement/spec.md`; `openspec/specs/inplace-overview-basin-detail/spec.md`; `openspec/specs/map-feature-popups/spec.md`; `openspec/specs/met-station-cluster-layer/spec.md` |

## Materialization Rows

Line-level listing would be noisy, so selected files are summarized by
check ID and family count from the before and after entropy reports.

| Path | Line/check ID/family | Selected vs remaining | Marker scope | Marker status | current_authority | superseded_by | Before budget status | After budget status | Owner area | Follow-up issue or #688 | Disposition reason |
|---|---|---|---|---|---|---|---|---|---|---|---|
| `docs/archived/legacy-slurm-templates.md` | `placeholder-path-token` x2 | selected | whole-document | archived | `slurm-current` | `slurm-current` | budget-counted x2 | budget-counted x0; complete-marker allowlisted x2 | slurm_gateway | #681 complete | Historical Slurm template path evidence and the migration table are retained, while current guidance points to gateway/template authority and role/topology context. |
| `openspec/changes/archive/2026-06-18-governance-2-legacy-dead-code-retirement/proposal.md` | `placeholder-path-token` x2 | selected | whole-document | archived | `gov2-current` | `gov2-current` | budget-counted x2 | budget-counted x0; complete-marker allowlisted x2 | shared_contract | #681 complete | Archived Governance-2 proposal remains evidence for retired placeholder disposition. |
| `openspec/changes/archive/2026-06-18-governance-2-legacy-dead-code-retirement/design.md` | `placeholder-path-token` x16 | selected | whole-document | archived | `gov2-current` | `gov2-current` | budget-counted x16 | budget-counted x0; complete-marker allowlisted x16 | shared_contract | #681 complete | Archived Governance-2 design remains evidence for retired path policy and cleanup rationale. |
| `openspec/changes/archive/2026-06-18-governance-2-legacy-dead-code-retirement/tasks.md` | `placeholder-path-token` x20 | selected | whole-document | archived | `gov2-current` | `gov2-current` | budget-counted x20 | budget-counted x0; complete-marker allowlisted x20 | shared_contract | #681 complete | Archived Governance-2 task evidence is retained for audit traceability. |
| `openspec/changes/archive/2026-06-18-governance-2-legacy-dead-code-retirement/specs/legacy-dead-code-retirement/spec.md` | `placeholder-path-token` x4 | selected | whole-document | archived | `gov2-current` | `gov2-current` | budget-counted x4 | budget-counted x0; complete-marker allowlisted x4 | shared_contract | #681 complete | Archived OpenSpec delta is retained; current legacy/dead-code authority is the active spec and governance inventory. |
| `openspec/changes/archive/2026-06-18-m26-unified-map-display/proposal.md` | `stale-display-route-token` x10 | selected | whole-document | archived | `m26-current` | `m26-current` | budget-counted x10 | budget-counted x0; complete-marker allowlisted x19 | display_readonly | #681 complete | Archived M26 proposal keeps old route evidence while current capability authority is the active M26 specs/runbooks. |
| `openspec/changes/archive/2026-06-18-m26-unified-map-display/design.md` | `stale-display-route-token` x8 | selected | whole-document | archived | `m26-current` | `m26-current` | budget-counted x8 | budget-counted x0; complete-marker allowlisted x8 | display_readonly | #681 complete | Archived M26 design remains evidence for the route-convergence decision. |
| `openspec/changes/archive/2026-06-18-m26-unified-map-display/tasks.md` | `stale-display-route-token` x7 | selected | whole-document | archived | `m26-current` | `m26-current` | budget-counted x7 | budget-counted x0; complete-marker allowlisted x16 | display_readonly | #681 complete | Archived M26 task evidence is retained; current display capabilities live in the active M26 specs/runbooks. |
| `openspec/changes/archive/2026-06-18-m26-unified-map-display/issue-432-worklog.md` | no selected route/path finding | selected | whole-document | archived | `m26-current` | `m26-current` | none | none; marker materialized | display_readonly | #681 complete | File is in the required first materialization set and is marked consistently even without a selected budget finding. |
| `openspec/changes/archive/2026-06-18-m26-unified-map-display/worklogs/issue-337-worklog.md` | `stale-display-route-token` x12 | selected | whole-document | archived | `m26-current` | `m26-current` | budget-counted x12 | budget-counted x0; complete-marker allowlisted x12 | display_readonly | #681 complete | Worklog preserves route-convergence implementation history but is non-current. |
| `openspec/changes/archive/2026-06-18-m26-unified-map-display/worklogs/issue-338-worklog.md` | `stale-display-route-token` x2 | selected | whole-document | archived | `m26-current` | `m26-current` | budget-counted x2 | budget-counted x0; complete-marker allowlisted x2 | display_readonly | #681 complete | Worklog preserves basin/detail routing history; current same-capability authority includes the active basin/detail spec. |
| `openspec/changes/archive/2026-06-18-m26-unified-map-display/worklogs/issue-339-worklog.md` | no selected route/path finding | selected | whole-document | archived | `m26-current` | `m26-current` | none | none; marker materialized | display_readonly | #681 complete | File is in the required first materialization set; current same-capability authority includes the active station-layer spec. |
| `openspec/changes/archive/2026-06-18-m26-unified-map-display/worklogs/issue-340-worklog.md` | `stale-display-route-token` x7 | selected | whole-document | archived | `m26-current` | `m26-current` | budget-counted x7 | budget-counted x0; complete-marker allowlisted x7 | display_readonly | #681 complete | Worklog preserves popup implementation evidence; current same-capability authority includes the active popup spec. |
| `openspec/changes/archive/2026-06-18-m26-unified-map-display/worklogs/issue-341-worklog.md` | `stale-display-route-token` x12 | selected | whole-document | archived | `m26-current` | `m26-current` | budget-counted x12 | budget-counted x0; complete-marker allowlisted x14 | display_readonly | #681 complete | Worklog preserves legacy page retirement evidence; current authority is active M26 route/display specs. |
| `openspec/changes/archive/2026-06-18-m26-unified-map-display/worklogs/node27-live-receipt.md` | `stale-display-route-token` x7 | selected | whole-document | archived | `m26-current` | `m26-current` | budget-counted x7 | budget-counted x0; complete-marker allowlisted x7 | display_readonly | #681 complete | Historical node-27 receipt is retained as M26 evidence, not current live validation guidance. |
| `openspec/changes/archive/2026-06-18-m26-unified-map-display/specs/legacy-display-page-retirement/spec.md` | `stale-display-route-token` x1 | selected | whole-document | archived | `m26-current` | `m26-current` | budget-counted x1 | budget-counted x0; complete-marker allowlisted x4 | display_readonly | #681 complete | Archived OpenSpec delta is retained; current legacy display page authority is the active spec. |
| `openspec/changes/archive/2026-06-18-m26-unified-map-display/specs/single-map-shell-routing/spec.md` | `stale-display-route-token` x2 | selected | whole-document | archived | `m26-current` | `m26-current` | budget-counted x2 | budget-counted x0; complete-marker allowlisted x16 | display_readonly | #681 complete | Archived OpenSpec delta is retained; current single-map routing authority is the active spec. |
| `openspec/changes/archive/2026-06-18-m26-unified-map-display/specs/inplace-overview-basin-detail/spec.md` | `stale-display-route-token` x2 | selected | whole-document | archived | `m26-current` | `m26-current` | budget-counted x2 | budget-counted x0; complete-marker allowlisted x2 | display_readonly | #681 complete | Archived delta preserves old basin route context; current same-capability authority includes the active basin/detail spec. |
| `openspec/changes/archive/2026-06-18-m26-unified-map-display/specs/map-feature-popups/spec.md` | no selected route/path finding | selected | whole-document | archived | `m26-current` | `m26-current` | none | none; marker materialized | display_readonly | #681 complete | File is in the required first materialization set; current same-capability authority includes the active popup spec. |
| `openspec/changes/archive/2026-06-18-m26-unified-map-display/specs/met-station-cluster-layer/spec.md` | `stale-display-route-token` x1 | selected | whole-document | archived | `m26-current` | `m26-current` | budget-counted x1 | budget-counted x0; complete-marker allowlisted x1 | display_readonly | #681 complete | Archived delta preserves M26 station-layer route context; current same-capability authority includes the active station-layer spec. |
| `docs/archived/**` and `openspec/changes/archive/**` outside the selected #681 set | `placeholder-path-token` x38 remaining archive family | remaining | none in #681 | unmarked/incomplete until future work | see family-specific current authority before acting | none in #681 | budget-counted x38 | budget-counted x38 | shared_contract | #688 | Outside the first governed materialization set; remains visible for final structural entropy verification. |
| `docs/archived/**` and `openspec/changes/archive/**` outside the selected #681 set | `stale-display-route-token` x300 remaining archive family | remaining | none in #681 | unmarked/incomplete until future work | see family-specific current authority before acting | none in #681 | budget-counted x300 | budget-counted x300 | display_readonly/shared_contract | #688 | Outside the first governed materialization set; remains visible for final structural entropy verification. |

No selected-set file remains unresolved after #681 materialization. Remaining
archive route/path findings are intentionally left budget-counted and assigned
to #688 rather than hidden by broad archive-path suppression.
