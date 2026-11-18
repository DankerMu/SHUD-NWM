# Tasks — retire-retry-residue (#3183)

## Risk packs

| Pack | Selected | Reason / mapping |
|---|---|---|
| File IO / path safety / overwrite | yes | a private file writer is removed from the journal event path: E2, E4 |
| Concurrency / shared state / ordering | yes | event write under the journal lock loses one step: E2 (records byte-identical), E3 |
| Legacy compatibility / examples | yes | old on-disk `private/runtime-root-recovery` records stay inert; status-vocab parity anchors: E3 |
| Resource limits / large input / discovery | yes | two guard exemptions removed: E1 |
| Documentation / migration notes | yes | ADR 0009 census, spec deltas: E3 (census guard), E6 |
| Error handling / rollback / partial outputs | yes | four raise sites of the deleted private write disappear; each input class checked as already-rejected-earlier or now-accepted: What 1, E7 |
| Others | no | no API, schema, config or runtime-surface change |

- [x] 1.1 Delete the write-only lane, its retry.py tail and the dead seam test. Update the journal tests, the cluster-map fixture, the consistency-test helpers and the docstring. Record the failure-path input classes (What 1).
- [x] 1.2 Delete `_PENDING_JOB_STATUSES` and its two parity rows (What 2). Remove the two guard entries (What 3).
- [x] 1.3 Refresh the ADR 0009 census from the guard's computed set (What 4).
- [x] 1.4 Add the upsert placeholder `error_message` pin (What 5).
- [x] 1.5 Evidence: E1-E4 locally.
- [x] 1.6 E5 on node-27 (orchestrator), E6, and the E7 classification in the PR body.
