# Tasks

## 1. Store
- [x] 1.1 Worker pool with `STATION_FETCH_CONCURRENCY`, context-ordered merge, valves raised, `unloadedBasinCount`
- [x] 1.2 Correct the cap comments
- [x] 1.3 Store tests (evidence floor list)

## 2. Hook
- [x] 2.1 Expose `unloadedBasinCount`; status note wording
- [x] 2.2 Hook tests

## 3. Docs
- [x] 3.1 Correct the two runbooks that describe the 5000 / 50 truncation as current

## 4. Verification
- [x] 4.1 `pnpm test`, `typecheck`, `check:types`, `build`, full mocked Playwright lane
- [x] 4.2 `openspec validate load-all-basin-met-stations --strict --no-interactive`
- [ ] 4.3 node-27 live receipt (orchestrator)
