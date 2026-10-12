# Tasks

## 1. Reset
- [x] 1.1 Delete the unlayered `font: inherit` rule in `apps/frontend/src/index.css`
- [x] 1.2 Build check: no unlayered `font:inherit` for the four elements in the built CSS

## 2. Consequences
- [x] 2.1 Measurement script and before/after table (design D3)
- [x] 2.2 Class fixes under design D1's exceptions
- [x] 2.3 Remove the `!` workarounds (design D2)
- [x] 2.4 Re-pin moved desktop font baselines, each listed
- [x] 2.5 Utility-works assertion with mutation (design D4)

## 3. Verification
- [x] 3.1 `pnpm test`, `typecheck`, `check:types`, `build`, full mocked Playwright lane
- [x] 3.2 `openspec validate layer-font-inherit-reset --strict --no-interactive`
- [x] 3.3 node-27 browser receipt with screenshots (orchestrator)

Merge gate (not a task box): owner visual sign-off on the before/after table and the node-27 screenshots.
