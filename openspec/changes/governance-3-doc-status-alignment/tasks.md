## 0. Dependency gate

- [ ] 0.1 Confirm `governance-0-ci-contract-baseline` is merged and green, or record an explicit maintainer waiver listing current red checks.

## 1. Document authority model

- [ ] 1.1 Add `docs/governance/DOC_STATUS.md` with document statuses and conflict-resolution order.
- [ ] 1.2 Link `DOC_STATUS.md` from README or another current entrypoint.
- [ ] 1.3 Mark `IMPLEMENTATION_PLAN.md` as historical or move it under `docs/archived/` with a root pointer.

## 2. High-impact stale docs

- [ ] 2.1 Update `CLAUDE.md` current active milestone from stale M23 wording to current governance/P0/P1 priorities.
- [ ] 2.2 Update `progress.md` and `docs/runbooks/node-27-bringup-checklist.md` for #343/#351 live MVT facts and remaining #342 station-MVT status.
- [ ] 2.3 Update both `infra/env/display.example` and `infra/compose.display.yml` to include/pass through `NHMS_ENABLE_LIVE_POSTGIS_MVT=true`.
- [ ] 2.4 Verify stale wording with `rg -n "#343|M23|NHMS_ENABLE_LIVE_POSTGIS_MVT"`.
- [ ] 2.5 Verify compose pass-through with `docker compose --env-file infra/env/display.example -f infra/compose.display.yml config | rg NHMS_ENABLE_LIVE_POSTGIS_MVT`.

## 3. Bugs ledger

- [ ] 3.1 Convert `docs/bugs.md` entries to include `status`, `owner_area`, `resolved_by`/`superseded_by`, and `retest_command`.
- [ ] 3.2 Triage at least BUG-003, BUG-007, BUG-008, BUG-009, BUG-010, BUG-011, BUG-012, and BUG-013.
- [ ] 3.3 Link any still-open bug to a GitHub issue or explicit owner area.

## 4. Agent/artifact ownership

- [ ] 4.1 Decide whether `.agents/skills/**`, `.codex/reviews/**`, and `apps/frontend/artifacts/**` are project assets or local/generated artifacts.
- [ ] 4.2 Align `.gitignore`, `.dockerignore`, and `progress.md` with that decision.
- [ ] 4.3 Verify tracked file list and ignore behavior after the policy change.
