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

### D2. Fix high-impact stale facts before broad docs cleanup

The first docs PR should update the documents most likely to mislead implementation: active milestone, node-27 live MVT facts, display env config, and current issue status.

### D3. Convert bugs into a ledger

`docs/bugs.md` should stop being a chronological pile. Each bug should have status, owner area, resolved/superseded evidence, and retest command.

### D4. Decide tracked local/agent assets explicitly

`.agents`, `.codex`, and `apps/frontend/artifacts` must be either project assets or local/generated artifacts. The repo should not say "do not stage" for assets it intentionally tracks.

## Four-Role Coverage

| Role | Documentation focus |
|---|---|
| `compute_control` | node-22 runbooks, production daemon vs diagnostic QHH lane, scheduler evidence. |
| `display_readonly` | node-27 checklist, live MVT receipt, readonly DB/live browser evidence, display env config. |
| `slurm_gateway` | standalone gateway docs and legacy template notes. |
| `shared_contract` | OpenAPI/generated types, DB specs, bugs ledger, docs authority hierarchy. |

## Risks / Mitigations

- **Risk: docs update changes claims without evidence.** Mitigation: every status change must link to PR, issue, runbook, test, or retest command.
- **Risk: archiving root docs breaks discoverability.** Mitigation: leave a small root pointer if `IMPLEMENTATION_PLAN.md` is archived.
- **Risk: `.agents` ownership decision affects tooling.** Mitigation: make the choice explicit before changing ignore rules.

## Verification

- Markdown lint if enabled by CI.
- `rg` checks for stale `#343` open wording and M23 current milestone wording.
- `docker compose --env-file infra/env/display.example -f infra/compose.display.yml config` if display env/compose changes.
