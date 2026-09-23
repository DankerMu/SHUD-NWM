## ADDED Requirements

### Requirement: The cross-PR round-ceiling memory SHALL have a tracked, vocabulary-closed writer

`scripts/review_gate.py` SHALL be the only writer of `.review-gate-issues.json`. It SHALL write entries only under the `issues` map. It SHALL accept a close outcome only from `OUTCOMES` (`merged`, `superseded-by-split`, `abandoned`, `descoped`), with no default value. Loading SHALL fold a bare top-level key into `issues` when that is unambiguous, and SHALL exit nonzero and print both records when the bare key conflicts with its `issues` entry. `record` SHALL NOT modify `gateEntries`, SHALL add a ceiling PR at most once, and SHALL leave an entry byte-identical when re-run with identical arguments. `check` SHALL exit 2 when the issue already has a ceiling PR other than the one given by the optional `--pr`. The repository instructions SHALL name the `check` and `record` invocations.

#### Scenario: a conflicting bare key refuses to load

- **WHEN** the memory holds a bare top-level `"1660"` with `gateEntries: 1` beside `issues["1660"]` with `gateEntries: 0`
- **THEN** every subcommand exits nonzero and prints both records

#### Scenario: an out-of-vocabulary outcome cannot be written

- **WHEN** `record` is invoked with `--outcome closed`, or without `--outcome`
- **THEN** argument parsing fails and the file is unchanged

#### Scenario: re-recording the same close is a no-op

- **WHEN** `record --issue N --pr P --rounds 2 --outcome merged` runs twice
- **THEN** the file bytes after the second run equal those after the first, and `gateEntries` is unchanged from before the first run

#### Scenario: a prior ceiling escalates a successor PR

- **WHEN** `issues["N"].ceilingPrs` contains PR 100 and `check --issue N --pr 101` runs
- **THEN** it exits 2 and names PR 100

### Requirement: The loop-log attribution audit SHALL be tracked and executed

The #2036 attribution audit SHALL live at `scripts/governance/loop_log_audit.py` with the `rotation_sample` and `Attribution(core, rotated, final_review, skipped)` semantics. `tests/test_loop_log_audit_attribution.py` SHALL import it as a normal module and SHALL execute with zero skips in a clean checkout.

#### Scenario: the attribution suite runs in CI

- **WHEN** `uv run pytest -q -rs tests/test_loop_log_audit_attribution.py` runs in a clean checkout
- **THEN** every case executes and none is skipped
