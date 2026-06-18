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

### D3. Convert bugs into a governance ledger

`docs/bugs.md` must stop being a chronological stale list. #369 converts it
into a ledger with consistent fields, a small status vocabulary, and
role-oriented ownership. Required fields:

- `status`: `open`, `resolved`, `superseded`, `stale-needs-repro`, or
  `archived`.
- `owner_area`: one of `compute_control`, `display_readonly`,
  `slurm_gateway`, or `shared_contract`.
- `resolved_by` or `superseded_by`: PR, issue, runbook, commit, or current
  source-of-truth evidence when applicable.
- `retest_command`: concrete command or explicit live receipt needed to
  re-check the bug.
- `evidence`: existing artifact, doc, issue, PR, test, or source path backing
  the status.

At minimum, BUG-20260527-003 and BUG-20260527-007 through
BUG-20260527-013 must be triaged. Still-open bugs must link to a GitHub issue
or have an explicit owner area and retest command.

### D4. Govern tracked agent and generated artifact ownership

Governance-3D resolves the tracked/local contradiction by classifying path
families rather than whole directories:

- `.agents/skills/**` that is already tracked is a reviewed project asset. New
  or changed project skills require normal PR review. Local installed skills
  and scratch skill work remain local/generated unless a PR explicitly promotes
  them.
- Unpromoted `.agents/skills/**` additions remain ignored because they are
  local/generated installed or scratch skill copies, not governed project
  skills in this repository snapshot. Promoting a new project skill file
  requires intentional force-add and PR review.
- `.codex/tmp/`, `.codex/cache/`, `.codex/evidence/`, and new
  `.codex/reviews/**` files are local/generated workflow evidence by default.
  Existing tracked `.codex/reviews/**` fixtures remain historical project
  evidence, but that history does not make future review outputs tracked by
  default.
- `apps/frontend/artifacts/m11-*.png` remains tracked historical visual
  evidence. New files under `apps/frontend/artifacts/**` are local/generated
  visual evidence by default unless a future issue explicitly promotes them.
- Root `artifacts/` remains local/generated production or review evidence and
  stays ignored.
- Docker build context should exclude agent/evidence directories that are not
  runtime inputs, including `.agents`, `.codex`, and frontend visual artifacts.

`progress.md` and `docs/governance/DOC_STATUS.md` must state this policy without
saying that all `.agents`, all `.codex`, or all frontend artifacts are
untracked. Ignore rules must prevent accidental staging of new generated
evidence while preserving already tracked project assets.

Fixture level: expanded
Project profile: NHMS
Repair intensity: medium
Change surface:
- `.gitignore`, `.dockerignore`, `progress.md`,
  `docs/governance/DOC_STATUS.md`, and this OpenSpec change.
Must preserve:
- Existing tracked `.agents/skills/**`, tracked `.codex/reviews/**` fixtures,
  and tracked `apps/frontend/artifacts/m11-*.png` remain visible in
  `git ls-files`.
- Root `artifacts/` remains ignored while `services/artifacts/*.py` remains
  trackable.
Must add/change:
- Contributor guidance distinguishes reviewed project assets from new local
  generated artifacts.
- `git check-ignore -v` demonstrates new generated review/evidence/frontend
  artifact paths are ignored by versioned `.gitignore` rules, not only by local
  `.git/info/exclude`.
- `.dockerignore` directly excludes non-runtime agent/evidence directories from
  Docker build context.

Risk packs considered:
- Public API / CLI / script entry: not selected - no runtime entrypoint change.
- Config / project setup: selected - `.gitignore` and `.dockerignore` govern
  contributor and build-context behavior.
- File IO / path safety / overwrite: selected - path-family ignore policy
  affects whether generated evidence is accidentally staged or shipped.
- Schema / columns / units / field names: not selected - no data schema change.
- Auth / permissions / secrets: not selected - no secret handling change.
- Concurrency / shared state / ordering: not selected - no runtime state change.
- Resource limits / large input / discovery: selected - Docker context exclusion
  limits generated evidence and agent assets from build contexts.
- Legacy compatibility / examples: selected - existing tracked historical
  evidence must not disappear.
- Error handling / rollback / partial outputs: not selected - no runtime
  rollback behavior.
- Release / packaging / dependency compatibility: selected - Docker context
  contents are packaging inputs.
- Documentation / migration notes: selected - docs are the primary policy
  surface.
Domain packs:
- Published NHMS artifacts / display identity: selected - root `artifacts/` and
  frontend visual evidence are artifact/evidence path families, though no
  runtime publish identity changes.
- Other NHMS domain packs: not selected - no geospatial, forcing, SHUD, Slurm,
  provider, DB, or manifest behavior changes.

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
- `rg` checks that required bug IDs have `status`, `owner_area`, `evidence`,
  and `retest_command` fields in `docs/bugs.md`.
