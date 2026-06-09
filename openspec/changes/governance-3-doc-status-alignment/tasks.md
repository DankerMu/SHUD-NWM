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

- [ ] Deferred to #368.

## 3. Bugs ledger

- [ ] Deferred to #369.

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
