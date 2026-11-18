# Tasks — retire-state-clone-activation-hook (#3189)

## Risk packs

| Pack | Selected | Reason / mapping |
|---|---|---|
| Legacy compatibility / examples | yes | API callers never pass `cold_start_approval`; result shape loses a never-emitted block: E2, E4 |
| Error handling / rollback / partial outputs | yes | audit-failure rollback and no-republish preserved via new injection: E3 |
| Concurrency / shared state / ordering | yes | activation single transaction + scope lock untouched: E3, E5 |
| Others | no | no schema/API/config change |

- [x] 1.1 Part 1 production deletions (What part 1 items 1-6), keeping the load-bearing block.
- [x] 1.2 Test rewrites per the table; delete the hook suite.
- [x] 1.3 Selector tables/pins, durations, large-file guard, ADR note.
- [x] 1.4 Part 2 comment rewording.
- [x] 1.5 E1-E4 locally.
- [ ] 1.6 E5 on node-27 (orchestrator), E6, E7.
