## Context

The audit found several concrete drifts:

- `CLAUDE.md` still names M23 as the current active milestone even though M25/M26 landed.
- `progress.md` and `node-27-bringup-checklist.md` still describe `#343` as pending/open, while `display-readonly-live-mvt.md` records the 2026-06-08 live MVT receipt.
- `infra/env/display.example` lacks `NHMS_ENABLE_LIVE_POSTGIS_MVT=true` even though the runbook requires it.
- `docs/bugs.md` contains many `open` issues that appear resolved, superseded, or stale.
- `IMPLEMENTATION_PLAN.md` is a 2026-05-06 design-freeze plan, but still sits as root-level current-looking guidance.
- `progress.md` says not to stage `.agents/`, while many `.agents/skills/**` files are tracked.

## Decisions

### D1. Add a document status source of truth

`docs/governance/DOC_STATUS.md` should classify documents by authority and freshness rules:

- current entrypoint
- current runbook
- current validation matrix
- architecture/spec
- module decomposition
- historical baseline
- superseded
- archived

### D2. Align high-impact stale facts in Governance-3B

Active milestone, node-27 live MVT facts, display env config, and current issue
status are governed by #368. This slice updates only the high-impact facts that
can mislead current development or display deployment:

- `CLAUDE.md` must stop presenting M23 as the current active milestone.
- `progress.md` and `node-27-bringup-checklist.md` must stop presenting #343
  as the unresolved live PostGIS MVT root cause after #351 closed #343 with
  the 2026-06-08 live receipt.
- #342 station-MVT must remain separate and open.
- #389 must route the remaining bbox/framing popup live-click browser evidence
  gap separately from #342 station-MVT and #343 live MVT closure.
- `infra/env/display.example` and `infra/compose.display.yml` must expose
  `NHMS_ENABLE_LIVE_POSTGIS_MVT` for display readonly deployments.
- Display config updates must preserve readonly safety: `display_readonly`
  role, disabled control mutations, readonly DB intent, compose `read_only`,
  readonly published bind mount, and no new Slurm/control-plane capabilities.

### D3. Defer bugs ledger conversion to Governance-3C

`docs/bugs.md` ledger conversion remains required for the Governance-3 epic, but
it is not part of the #367 document authority PR.

### D4. Defer tracked local/agent asset ownership to Governance-3D

`.agents`, `.codex`, and `apps/frontend/artifacts` ownership remains required
for the Governance-3 epic, but it is not part of #367.

## Four-Role Coverage

| Role | Documentation focus |
|---|---|
| `compute_control` | node-22 runbooks, production daemon vs diagnostic QHH lane, scheduler evidence. |
| `display_readonly` | node-27 checklist, live MVT receipt, readonly DB/live browser evidence, display env config. |
| `slurm_gateway` | standalone gateway docs and legacy template notes. |
| `shared_contract` | OpenAPI/generated types, DB specs, bugs ledger, docs authority hierarchy. |

## Risks / Mitigations

- **Risk: document status claims overreach into stale fact fixes.** Mitigation:
  #367 only defines the authority model and marks the historical implementation
  plan status; concrete stale fact fixes stay in G3-B/C/D.
- **Risk: archiving root docs breaks discoverability.** Mitigation: leave a small root pointer if `IMPLEMENTATION_PLAN.md` is archived.

## Verification

- Markdown lint if enabled by CI.
- `rg` check that a current entrypoint links `docs/governance/DOC_STATUS.md`.
- `rg` check that `IMPLEMENTATION_PLAN.md` clearly says historical/superseded,
  or that a root pointer exists when the historical plan is archived.
- `rg` checks for concrete stale/current facts: M23 is no longer current,
  #351/#343 live MVT closure is stated, #342 remains separate/open, and display
  readonly safety keys remain present.
- `docker compose --env-file infra/env/display.example -f infra/compose.display.yml config`
  shows `NHMS_ENABLE_LIVE_POSTGIS_MVT`.
