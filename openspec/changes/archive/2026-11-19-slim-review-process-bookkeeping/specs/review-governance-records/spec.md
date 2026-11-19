## MODIFIED Requirements

### Requirement: The cross-PR round-ceiling memory SHALL have a tracked, vocabulary-closed writer

`scripts/review_gate.py` SHALL be the only writer of `.review-gate-issues.json`. It SHALL write entries only under the `issues` map. It SHALL accept a close outcome only from `OUTCOMES` (`merged`, `superseded-by-split`, `abandoned`, `descoped`), with no default value. Loading SHALL fold a bare top-level key into `issues` when that is unambiguous, and SHALL exit nonzero and print both records when the bare key conflicts with its `issues` entry. `record` SHALL NOT modify `gateEntries`, SHALL add a ceiling PR at most once, and SHALL leave an entry byte-identical when re-run with identical arguments. `record` SHALL NOT persist a `merged` close that carries no `--ceiling`: it SHALL exit 0, say that nothing was recorded, and leave the file bytes unchanged. `check` SHALL exit 2 when the issue already has a ceiling PR other than the one given by the optional `--pr`. The repository instructions SHALL name the `check` and `record` invocations and SHALL state that `record` runs only for a non-`merged` close or a ceiling hit.

#### Scenario: a conflicting bare key refuses to load

- **WHEN** the memory holds a bare top-level `"1660"` with `gateEntries: 1` beside `issues["1660"]` with `gateEntries: 0`
- **THEN** every subcommand exits nonzero and prints both records

#### Scenario: an out-of-vocabulary outcome cannot be written

- **WHEN** `record` is invoked with `--outcome closed`, or without `--outcome`
- **THEN** argument parsing fails and the file is unchanged

#### Scenario: re-recording the same close is a no-op

- **WHEN** `record --issue N --pr P --rounds 2 --outcome descoped` runs twice
- **THEN** the file bytes after the second run equal those after the first, and `gateEntries` is unchanged from before the first run

#### Scenario: a clean merge is not recorded

- **WHEN** `record --issue N --pr P --rounds 1 --outcome merged` runs without `--ceiling`
- **THEN** it exits 0, reports that nothing was recorded, and the file bytes are unchanged; when the file did not exist it still does not exist

#### Scenario: a merge that hit the ceiling is recorded

- **WHEN** `record --issue N --pr P --rounds 3 --outcome merged --ceiling` runs
- **THEN** `issues["N"].closed` contains the `merged` record and `issues["N"].ceilingPrs` contains P

#### Scenario: a prior ceiling escalates a successor PR

- **WHEN** `issues["N"].ceilingPrs` contains PR 100 and `check --issue N --pr 101` runs
- **THEN** it exits 2 and names PR 100
