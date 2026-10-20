## Why

`apps/frontend/src/stores/__tests__/modelAssets.test.ts` is 1057 lines long and has no `.large-file-guard.json` exclude. It has been over the limit since the root commit `af49ced22`. The large-file guard checks every staged file in full, so in any session where the hook is wired up, a commit that stages this file is blocked (#3046). That is the same latent blocker #2986 removed for `M11StationForcingPopup.test.tsx`. It is now the only non-exempted, line-meaningful file over the limit under `apps/frontend`.

## What Changes

- Split the suite by topic into `modelAssets<Topic>.test.ts` files, each under 1000 lines, following the #2986 method:
  - Shared helpers and constants move into the non-collected module `__tests__/modelAssetsFixtures.ts`.
  - Every new test file declares its own `vi.mock('@/api/client', ...)` and `beforeEach`.
  - Only physical moves: no assertion changes, no production code changes, no new guard exclude.

## Triage

```text
#3046  Issue type: test (split) | Fixture level: compact | Upstream: absent
       Blast radius: one frontend test file | Risk packs: Legacy compatibility (collected set + assertions unchanged)
       Evidence floor: collected-case set equality (36, it.each expanded), expect( multiset equality (110),
                       every file < 1000 lines, no new exclude, vitest/typecheck/lint green, prod diff empty
```

`design.md` is omitted. The issue is compact, and its exact approach is in the issue body and `tasks.md`.

## Impact

- Tests: `apps/frontend/src/stores/__tests__/modelAssets*.ts`.
- Production code: none.
- `.large-file-guard.json`: unchanged. The path was never exempted.
