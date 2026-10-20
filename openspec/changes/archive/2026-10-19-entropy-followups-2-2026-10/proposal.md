## Why

Epic #2919 (entropy-burndown-2026-10) and its first follow-up batch (`entropy-followups-2026-10`, archived) left four more entropy follow-ups that can be verified locally or in CI. None of them waits on node-27:
- #3042: the CI selector never routes the governed facades or their compatibility inventories to the facade-guard current-repo tests. PR #3041's inventory gap only surfaced in the post-merge style full run.
- #3034: journal-facade patchers the selector still misses after #3028. The issue named four; the same criterion finds 14.
- #2986: `M11StationForcingPopup.test.tsx` is 1008 lines and only passes the large-file guard through an exclude added by PR #2985.
- #2978: `getApiErrorMessage` returns non-JSON error bodies verbatim, so proxy HTML or plain text reaches toasts and error states.

This change is their shared OpenSpec fixture. Each issue lands as its own PR, in this order: #3042, #3034, #2986, #2978.

## What Changes

- #3042: an additive selector rider. Edits to the four facade-guard inputs (`services/orchestrator/{scheduler,chain}.py`, `docs/governance/{SCHEDULER,CHAIN}_COMPATIBILITY_INVENTORY.md`) select the two facade-guard current-repo test nodes. Pins check that the nodes exist, that the mirrored path set equals `COMPATIBILITY_FACADE_CONFIGS`, and that nothing is duplicated.
- #3034: append every remaining journal-facade patcher to `FILE_ORCHESTRATION_JOURNAL_IMPORTER_TESTS` as node ids, and add them to the #3028 literal pin. The fixture review counted 14: the issue named 4, and the same criterion finds 10 more.
- #2986: split the popup test into topic files under 1000 lines. The collected `it` set and every assertion stay unchanged, and the exclude is removed.
- #2978: `getApiErrorMessage` fails closed. Any value that is neither an `Error` nor an object envelope returns `fallback`. The client-generated river-forecast contract messages stop going through it: `formatHydroMetStationSeriesMessage` is split so that they use only its string-formatting half.

## Triage

```text
#3042  Issue type: test (CI routing) | Fixture level: compact | Upstream: absent
       Blast radius: PR-lane selection only | Risk packs: Resource limits / discovery (mirror set must equal facade configs)
       Evidence floor: per-path selector output, 4 pins red-first, ruff, test_select_ci_tests
#3034  Issue type: test (CI routing) | Fixture level: compact | Blast radius: PR-lane selection only
       Evidence floor: selector output for facade + 7 siblings, literal pin extended, red-first
#2986  Issue type: test (split) | Fixture level: compact | Blast radius: one frontend test file
       Evidence floor: it-title set equality (24), each file < 1000 lines, exclude gone, vitest/typecheck green, prod diff empty
#2978  Issue type: bugfix | Fixture level: compact | Blast radius: user-facing error text in toasts/error states
       Risk packs: Error handling (fallback replaces raw bodies); Auth/secrets (leak of proxy banners/hostnames); Legacy compatibility (river-forecast contract messages unchanged)
       Evidence floor: response.test.ts rows updated + call-site test, bootstrap.test.ts:920-986 unchanged and green, mocked e2e regression unchanged, typecheck/lint/test
```

`design.md` is omitted. All four issues are compact and carry their exact approach in their issue bodies and `tasks.md`.

## Impact

- Code:
  - selector: `scripts/select_ci_tests*.py`, for #3042 and #3034;
  - `apps/frontend/src/api/response.ts` and `apps/frontend/src/lib/hydroMet/{stationSeries,riverForecast}.ts`, for #2978;
  - frontend test files and `.large-file-guard.json`, for #2986.
- No backend runtime change, no schema/API change, no node-22 or node-27 action.
