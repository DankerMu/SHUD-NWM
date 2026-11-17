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
| Documentation / migration notes | yes | spec REMOVED + ADDED: E6 |
| Others | no | no schema, config, IO path or display change |

- [ ] 1.1 Pin commit (unchanged production code):
  - run the verification set with `--with pytest-cov` branch coverage on `reconcile_reserved.py`, and record the
    exit → test map and the unhit branches;
  - add characterization tests for unhit exits and branches, the mutation-target tests (non-caching counting
    querier, advancing clock, `:860`/`:977` messages, `:496`-path quarantine basis), and the exact-signature recording
    write-order test, all in a new `tests/test_gateway_reconcile_<topic>.py` under 1000 lines with selector routing;
  - all of it is green on unchanged code.
- [ ] 1.2 Move commit: decompose per design.md, as verbatim slices in original order with only the allowed changes
  (proposal What 2). Add `reconcile_reserved_phases.py` to `RECONCILE_PARTITION_LEAVES` and to the selector rules.
  Remove the `.large-file-guard.json` entry once the module is ≤1000 lines.
- [ ] 1.3 Evidence: normalized body diff (E3), exit counts, E4 mutation reds, selector pins for any new module.
- [ ] 1.4 Run E1 and E5 locally.
