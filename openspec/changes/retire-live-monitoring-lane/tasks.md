# Tasks — retire-live-monitoring-lane (#3202)

## Risk packs

| Pack | Selected | Reason / mapping |
|---|---|---|
| Legacy compatibility / examples | yes | river-click/C4 untouched; no-mock guard still covers the shared spec: E1, E3 |
| CI / environment | yes | lane entry points removed; mocked-lane exclusion kept: E1, E2 |
| Others | no | no backend/API/RBAC change (E4) |

- [x] 1.1 Deletions (What 1-2), keeping What 3.
- [x] 1.2 Contract test edits (What 4), including the two new C4 negative-contract tests in `c4DisplayLane.test.tsx`.
- [x] 1.3 Docs (What 5).
- [x] 1.4 E1-E4 locally.
- [x] 1.5 E5 (orchestrator).
