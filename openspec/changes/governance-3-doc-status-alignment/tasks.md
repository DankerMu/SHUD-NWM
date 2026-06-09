## 0. Dependency gate

- [x] 0.1 Confirm `governance-0-ci-contract-baseline` is merged and green,
  or record an explicit maintainer waiver listing current red checks. Evidence:
  current branch contains merged baseline PRs #375 (`fix(contract): reconcile
  generated frontend API types`) and #376 (`chore(tooling): run Makefile
  Python targets via uv`), with governance follow-up commits #360-#366 already
  merged on top. `gh pr view 375 --json statusCheckRollup` shows CI check
  rollup success for #375, including `Detect changed areas` and `Frontend
  Build`; `gh run view 27154927963 --json status,conclusion,headSha,url`
  reports `status=completed`, `conclusion=success`,
  `headSha=11a0b7beca932fa9c727002b271cd2a077d9f729`. `gh pr view 376
  --json statusCheckRollup` shows CI check rollup success for #376, including
  `Detect changed areas`; path-scoped jobs are skipped as expected. `gh run
  view 27157572065 --json status,conclusion,headSha,url` reports
  `status=completed`, `conclusion=success`,
  `headSha=b9c7cbaedac84ee73a72e1df67a821ad9af0cc4f`.

## 1. Document authority model

- [x] 1.1 Add `docs/governance/DOC_STATUS.md` with document statuses and conflict-resolution order.
- [x] 1.2 Link `DOC_STATUS.md` from README or another current entrypoint.
- [x] 1.3 Mark `IMPLEMENTATION_PLAN.md` as historical or move it under `docs/archived/` with a root pointer.

## 2. High-impact stale docs

- [x] 2.1 Update `CLAUDE.md` active priorities away from stale M23 wording.
  Evidence: `CLAUDE.md` now lists #368/#369/#370 governance priorities and keeps
  #342 station-MVT as a separate open backend item.
- [x] 2.2 Update `progress.md` and `docs/runbooks/node-27-bringup-checklist.md`
  for #343/#351 live MVT facts and #342 remaining station-MVT status. Evidence:
  both files state #351 closed #343 with the 2026-06-08 live MVT receipt, while
  #342 and bbox/click automation gaps remain separate; bbox/framing popup live
  click evidence is now routed to #389.
- [x] 2.3 Ensure `docs/runbooks/display-readonly-live-mvt.md` stays
  consistent with the current 2026-06-08 live MVT receipt and display config.
  Evidence: runbook records #351 closure and the display.example/compose
  pass-through for `NHMS_ENABLE_LIVE_POSTGIS_MVT`.
- [x] 2.4 Update `infra/env/display.example` and `infra/compose.display.yml`
  to include/pass through `NHMS_ENABLE_LIVE_POSTGIS_MVT`. Evidence:
  `display.example` documents `NHMS_ENABLE_LIVE_POSTGIS_MVT=true`; compose passes
  `${NHMS_ENABLE_LIVE_POSTGIS_MVT:-false}` into `display-api`.
- [x] 2.4a Keep `NHMS_ENABLE_LIVE_POSTGIS_MVT` display-only in the static
  safety contract. Evidence: `scripts/validate_two_node_docker_runtime.py`
  includes the key only in display audited/runtime interpolation sets, and
  `tests/test_two_node_docker_runtime.py::test_static_checker_rejects_live_mvt_flag_as_compute_interpolation`
  proves the same key remains unapproved in compute compose interpolation.
- [x] 2.5 Verify stale wording and current issue state:
  - `rg -n "M23|当前活跃里程碑" CLAUDE.md`
  - `rg -n "#389|bbox|framing|popup live|live click|#351|#343|#342|NHMS_ENABLE_LIVE_POSTGIS_MVT" progress.md docs/runbooks/node-27-bringup-checklist.md docs/runbooks/display-readonly-live-mvt.md infra/env/display.example infra/compose.display.yml`
  - `rg -n "unresolved live MVT root cause|决定全国态 overlay 能否点亮|归 \\*\\*#343\\*\\*" progress.md docs/runbooks/node-27-bringup-checklist.md` returns no stale root-cause matches.
  Evidence: first and third commands returned no matches; the state check shows
  #351/#343 closure text, #342 separate/open text, #389 routing for
  bbox/framing popup live-click evidence, and the env/compose key.
- [x] 2.6 Verify compose pass-through:
  `docker compose --env-file infra/env/display.example -f infra/compose.display.yml config | rg NHMS_ENABLE_LIVE_POSTGIS_MVT`.
  Evidence: rendered compose includes `NHMS_ENABLE_LIVE_POSTGIS_MVT: "true"`.
- [x] 2.6a Verify source-of-truth GitHub issue/PR state:
  - `gh issue view 342 --json number,state,title,url` -> `state=OPEN`,
    title `[M26-6] 后端 station-MVT 点图层矢量瓦片端点(解耦·全国级)`.
  - `gh issue view 343 --json number,state,title,url,closed,closedAt,stateReason`
    -> `state=CLOSED`, `stateReason=COMPLETED`,
    `closedAt=2026-06-08T15:03:15Z`.
  - `gh pr view 351 --json number,state,mergedAt,title,url` ->
    `state=MERGED`, `mergedAt=2026-06-08T15:03:13Z`.
  - `gh issue view 389 --json number,state,title,url` -> `state=OPEN`,
    title `[Governance-3B follow-up] Route node-27 bbox/framing popup live-click evidence`.
- [x] 2.6b Verify added display env/compose lines did not add
  Slurm/Docker-socket/control-plane capability:
  `git diff --unified=0 origin/master...HEAD -- infra/env/display.example infra/compose.display.yml | rg '^\\+[^+]' | rg -n 'SLURM|DOCKER_HOST|docker\\.sock|/var/run/docker\\.sock|cap_add|privileged|network_mode:\\s*host|pid:\\s*host|ipc:\\s*host|volumes_from|env_file|WORKSPACE_ROOT|MUNGE'`.
  Evidence: command returns no matches over added lines.
- [x] 2.7 Verify display readonly safety keys remain present after config edits:
  `rg -n "NHMS_SERVICE_ROLE=display_readonly|NHMS_DISPLAY_DISABLE_CONTROL_MUTATIONS=true|nhms_display_ro|read_only: true|read_only: true|/api/v1/slurm" infra/env/display.example infra/compose.display.yml docs/runbooks/node-27-bringup-checklist.md`.
  Evidence: command returns `display_readonly`, control mutation blocker,
  `nhms_display_ro`, compose `read_only: true`, readonly bind mount, and
  `/api/v1/slurm/*` 404 checklist text.

## 3. Bugs ledger

- [x] 3.1 Define ledger status vocabulary and required fields in
  `docs/bugs.md`: `status`, `owner_area`, `resolved_by`/`superseded_by`,
  `evidence`, and `retest_command`. Evidence: `docs/bugs.md` now defines the
  governed status vocabulary, owner-area vocabulary, conditional
  `resolved_by`/`superseded_by` fields, `evidence`, and `retest_command`.
- [x] 3.2 Triage at least BUG-20260527-003, BUG-20260527-007,
  BUG-20260527-008, BUG-20260527-009, BUG-20260527-010, BUG-20260527-011,
  BUG-20260527-012, and BUG-20260527-013. Evidence: required entries are
  triaged as BUG-003 `open`/`shared_contract`, BUG-007
  `resolved`/`display_readonly`, BUG-008 `resolved`/`compute_control`, BUG-009
  `superseded`/`shared_contract`, BUG-010
  `stale-needs-repro`/`display_readonly`, BUG-011 `open`/`compute_control`,
  BUG-012 `superseded`/`display_readonly`, and BUG-013
  `open`/`slurm_gateway`.
- [x] 3.3 Mark each required bug `open`, `resolved`, `superseded`,
  `stale-needs-repro`, or `archived` with evidence from source code, tests,
  docs, runbooks, GitHub issues/PRs, or existing artifact paths. Evidence:
  `docs/bugs.md` cites original 2026-05-27 artifacts plus current source/tests,
  #291/#365/#233, M22/M24/M26 OpenSpec receipts, and live-display docs.
- [x] 3.4 Link still-open bugs to a GitHub issue when one exists; otherwise
  record an explicit `owner_area` and actionable `retest_command`. Evidence:
  required still-open entries BUG-003, BUG-011, and BUG-013 have explicit
  `owner_area` values and concrete retest commands; no direct existing GitHub
  issue was found for those required bug IDs.
- [x] 3.5 Verify the ledger fields:
  `rg -n "BUG-20260527-(003|007|008|009|010|011|012|013)|status:|owner_area:|resolved_by:|superseded_by:|evidence:|retest_command:" docs/bugs.md`.
  Evidence: command returned all required BUG headings and ledger fields,
  including `resolved_by` for resolved entries and `superseded_by` for
  superseded entries.
- [x] 3.6 Verify each required bug block has required fields, conditional
  resolution fields, retest shell syntax, and post-gate retest invariants.
  Evidence: `uv run --no-sync python scripts/validate_bugs_ledger.py` returned:

  ```text
  BUG-20260527-003: open shared_contract retest-shell-ok
  BUG-20260527-007: resolved display_readonly retest-shell-ok
  BUG-20260527-008: resolved compute_control retest-shell-ok
  BUG-20260527-009: superseded shared_contract retest-shell-ok
  BUG-20260527-010: stale-needs-repro display_readonly retest-shell-ok
  BUG-20260527-011: open compute_control retest-shell-ok
  BUG-20260527-012: superseded display_readonly retest-shell-ok
  BUG-20260527-013: open slurm_gateway retest-shell-ok
  ```

## 4. Agent/artifact ownership

- [ ] Deferred to #370.

## 5. Verification for #367

- [x] 5.1 Validate OpenSpec:
  `openspec validate governance-3-doc-status-alignment --strict --no-interactive`.
- [x] 5.2 Verify current entrypoint link:
  `rg -n "docs/governance/DOC_STATUS.md|DOC_STATUS" README.md progress.md CLAUDE.md`.
  This must return at least one match in each declared current entrypoint:
  `README.md`, `progress.md`, and `CLAUDE.md`.
- [x] 5.3 Verify implementation plan status:
  `rg -n "historical|superseded|archived|DOC_STATUS|current entrypoints" IMPLEMENTATION_PLAN.md docs/archived README.md`.
- [x] 5.4 Second-round PR #387 finding: qualify current-entrypoint status so
  `CLAUDE.md` remains linked as an entrypoint without claiming its deferred M23
  facts are fresh before #368.
