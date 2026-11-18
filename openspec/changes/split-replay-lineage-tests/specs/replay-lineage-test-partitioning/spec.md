## ADDED Requirements

### Requirement: Replay lineage tests stay within the line guard with collection identity preserved

The repository SHALL keep every collectible `tests/test_replay_lineage*.py` module, and any replay-lineage test support
module, at or below the configured 1,000-line guard, with no `.large-file-guard.json` entry for them. The partition
SHALL collect every pre-partition pytest case exactly once: the sorted `::test_name` suffix set, test bodies,
decorators and assertion oracles MUST stay equivalent. A private fixture used by two or more partitions SHALL live in a
non-collectible support module; a fixture used by one partition stays in it. No partition SHALL import another
partition. Docstrings and comments SHALL describe current behavior: clone
rows are written by the explicit clone tooling, not by model activation.

#### Scenario: Collection identity is preserved one-to-one

- **WHEN** the suite is collected with pytest before and after the partition
- **THEN** the sorted unique case suffixes after the first `::` are byte-identical, and every old suffix maps to exactly
  one new full node ID

#### Scenario: Every replay-lineage test module satisfies the guard

- **WHEN** `wc -l` runs over `tests/test_replay_lineage*.py` and the support module
- **THEN** every file is at or below 1,000 lines and `.large-file-guard.json` names none of them

#### Scenario: Targeted CI selects every partition

- **WHEN** targeted selection runs for a production path that previously selected `tests/test_replay_lineage.py`
  (`services/orchestrator/replay_lineage.py`, the `services/orchestrator/**` directory rule, or a model-registry owner
  module)
- **THEN** every replay-lineage partition is selected

#### Scenario: Support-module changes execute assertions

- **WHEN** the replay-lineage support module is the changed path
- **THEN** the selector returns both partitions plus `tests/test_select_ci_tests.py`, never the support module itself

#### Scenario: No retired-hook narrative remains

- **WHEN** `git grep -n -e "SUB-4 hook" -e "atomic-cutover transaction committed" tests/` runs
- **THEN** it returns no hit
