# Tasks — decompose-reserved-reconcile (#3176)

## Risk packs

| Pack | Selected | Reason / mapping |
|---|---|---|
| Slurm production lifecycle / mock-vs-real parity | yes | the submit-crash-window state machine (double-submit safety): E2, E4, E5 |
| Concurrency / shared state / ordering | yes | write order, per-pass capability cache, `now` call points: E2 write-order test, E4 |
| Error handling / rollback / partial outputs | yes | per-row quarantine, transient vs escaping raises: E4 exception-scope mutation |
| Legacy compatibility / examples | yes | import surface and patch seam: E4 seam mutation, E5 |
| Resource limits / large input / discovery | yes | large-file guard exclusion removed: E1 |
| CI routing / test selection | yes | new pin file and any new module: selector pins |
| Documentation / migration notes | yes | spec MODIFIED, line references: E6 |
| Others | no | no schema, config, IO path or display change |

- [ ] 1.1 Pin commit: run the verification set with coverage on `reconcile_reserved.py` (master code). Record the
  exit → test map, add characterization tests for unhit exits and the recording-store write-order test (new file,
  <1000 lines, selector-routed). Everything is green on unchanged code.
- [ ] 1.2 Move commit: decompose per design.md, as verbatim slices in original order with only the two allowed
  rewrites. Remove the `.large-file-guard.json` entry once the module is ≤1000 lines. Repoint line references.
- [ ] 1.3 Evidence: normalized body diff (E3), exit counts, E4 mutation reds, selector pins for any new module.
- [ ] 1.4 Run E1 and E5 locally.
