# Tasks — journal-write-complexity-limits (#3197)

## Risk packs

| Pack | Selected | Reason / mapping |
|---|---|---|
| File IO / path safety / overwrite | yes | refusal before the first byte, and the derived-file depth: E3 |
| Error handling / rollback / partial outputs | yes | tree byte-identical; batch and historical import refused whole: E3 |
| Legacy compatibility / examples | yes | round-tripping records unchanged; restore lanes unchecked; newly refused inputs classified: E3, What 7 |
| Resource limits / large input / discovery | yes | instance vs module limits, per-type headroom: E2, E4 |
| Concurrency / shared state / ordering | no | the check runs inside the existing write window and adds no state |
| Others | no | no API, schema or config change |

- [x] 1.1 Verify the depth offsets (What 1) and record the measured table (E2). Stop and report if an offset is not
  constant per record type.
- [x] 1.2 Shared complexity helper. Depth check with the per-type headroom and node check against the record (What 2,
  What 3), wired through `_validate_outgoing_record`. Confirm the closed lane list (What 4) and add the
  unchecked-by-design comments on the restore lanes.
- [x] 1.3 Historical-import pre-scan (What 5). Runbook line and cluster map as needed (What 6, What 8).
- [x] 1.4 E3 tests, E4 mutations, E1 and local E5.
- [ ] 1.5 E5 on node-27 (orchestrator), E6, and the What 7 classification in the PR.
