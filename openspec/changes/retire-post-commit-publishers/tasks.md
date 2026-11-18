# Tasks — retire-post-commit-publishers (#3204)

## Risk packs

| Pack | Selected | Reason / mapping |
|---|---|---|
| Legacy compatibility / examples | yes | production stores never registered publishers; result dict unchanged: E2, E4 |
| Error handling / rollback / partial outputs | yes | audit-failure rollback retained with a non-publisher observable: E3 |
| Concurrency / shared state / ordering | yes | activation transaction untouched; post-commit tail removed: E3, E5 |
| Others | no | no schema/API/config change |

- [x] 1.1 Production deletions 1-6, preserving the must-preserve list.
- [x] 1.2 Test dispositions per the table (moves keep assertions; new absence test).
- [x] 1.3 Selector/guard/durations/pyproject; runbooks.
- [x] 1.4 E1-E4 locally.
- [x] 1.5 E5 on node-27 (orchestrator), E6, E7.
