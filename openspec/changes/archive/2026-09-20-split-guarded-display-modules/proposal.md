## Triage

```text
Issue type: refactor
Fixture level: expanded
Upstream suggested level: absent (override: no upstream level; expanded because the
  change touches shared entrypoints — the display router, the runtime OpenAPI patch
  layer — plus legacy compatibility surfaces and module-level shared state)
Blast radius: display API route surface and its OpenAPI document; the guard/CI
  registries that decide what the PR lane runs; ~60 monkeypatch sites and 15
  source-text assertions that can go silently vacuous; the frontend overview
  contract and store consumed by 44 importers
Selected risk packs: Public API / CLI / script entry; Concurrency / shared state /
  ordering; Legacy compatibility / examples; Resource limits / large input /
  discovery; Documentation / migration notes (per-pack reasons in tasks.md §0)
Evidence floor: tests/test_openapi_drift.py green with openapi/nhms.v1.yaml
  unchanged; every affected backend suite green with test counts not below the
  pre-split baseline (210 + 38); tests/test_select_ci_tests.py green;
  tests/test_node27_connection_attribution{,_delegated}.py green; ruff green;
  frontend tsc + pnpm test + pnpm build green with zero test-file diff;
  every touched/new file <= 1000 lines with the six exemptions removed;
  node-27 live receipt; CI green
```

## Why

`.large-file-guard.json` currently exempts six display-plane modules that are all
hand-written source in the M27 change hot zone. Each exemption was added as the
hook's own escape hatch to unblock a commit (#2005 for `hydro_display.py`, #2073
for the `openapi_patching.py` / `test_api_contract.py` /
`test_hydro_display_mvt_scaling.py` trio, #2012 for the two frontend modules),
never as a repayment. With the guard blind, the files kept growing: measured on
`origin/master` (ed1d94ced) they are now

| file | issue | lines on master | at issue-filing time |
|---|---|---|---|
| `apps/api/routes/hydro_display.py` | #2026 | 1686 | 1001 |
| `apps/api/openapi_patching.py` | #2074 | 2203 | 2037 |
| `tests/test_api_contract.py` | #2074 | 2114 | 2114 |
| `tests/test_hydro_display_mvt_scaling.py` | #2074 | 4875 | 1957 |
| `apps/frontend/src/lib/m11/overviewDataContracts.ts` | #2102 | 1112 | 1467 |
| `apps/frontend/src/stores/overviewData.ts` | #2102 | 1268 | 1719 |

`test_hydro_display_mvt_scaling.py` alone grew 2.5x (1957 -> 4875) while exempt.
All blocking dependencies are resolved: #2005/#2007/#2009/#1983/#2012/#2014/#2015
are closed and PR #2073 is merged, so every exemption entry now exists on master
and can be removed.

## What Changes

- Split all six files below the 1000-line guard threshold with headroom, using
  compatibility facades (Python) and barrel re-exports (TypeScript) so importers
  keep working.
- Remove the six exemption entries from `.large-file-guard.json` in the same
  commit as the corresponding split, so no commit in this PR relies on an
  exemption for a file it also shrinks. `apps/frontend/src/api/types.ts` stays
  exempt: it is generated from `openapi/nhms.v1.yaml`.
- Update the CI targeted-test registries (`scripts/select_ci_tests.py`
  `GUARDED_MODULE_CLOSURES`, `OPENAPI_CONTRACT_TESTS`, `PathTestRule` entries)
  and the node-27 connection-attribution registries so every new module and test
  partition is selected and attributed rather than silently dropped.
- Preserve behavior exactly: no SQL text change, no route path change, no
  endpoint added or removed, no public API or export-name change, no CLI/env
  change.

## Deviation from upstream issue shape

#2026, #2074 and #2102 each prescribe one PR per file (#2074 is additionally
marked `needs-triage — 需分解`). The user directed a single batched PR for all
three issues, so this change carries all six files. Batching is recorded here as
a deliberate deviation; it is mitigated by one commit per file family, so each
family stays independently reviewable and revertable.

## Impact

- Affected source: `apps/api/routes/hydro_display.py` (+ new owner modules),
  `apps/api/openapi_patching.py` (+ new owner modules),
  `apps/frontend/src/lib/m11/overviewDataContracts.ts`,
  `apps/frontend/src/stores/overviewData.ts` (+ new submodules).
- Affected tests: `tests/test_hydro_display_mvt_scaling.py`,
  `tests/test_api_contract.py` (split into partitions), plus retargeted
  monkeypatch sites in `tests/test_display_mvt_cold_admission.py`,
  `tests/test_node27_connection_attribution.py` and any other test that patches
  a moved symbol.
- Affected registries: `.large-file-guard.json`, `scripts/select_ci_tests.py`,
  `tests/test_select_ci_tests.py`,
  `tests/test_node27_connection_attribution{,_delegated}.py`.
- Affected contracts: none by intent. `openapi/nhms.v1.yaml` must stay byte
  identical and `tests/test_openapi_drift.py` (whole-dict equality plus facade
  identity) is the enforcing oracle.
