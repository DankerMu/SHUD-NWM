## ADDED Requirements

### Requirement: QHH production-bootstrap tests remain complete under physical partitioning

The repository SHALL partition the QHH production-bootstrap pytest corpus into exactly
three collectible modules and one non-collectible helper whose individual line counts are
below 1,000, without changing `.large-file-guard.json` or retaining a collectible
compatibility shim. Every baseline case SHALL be collected exactly once after partitioning:
module prefixes may change, but all 66 unique `::test_name[param-id]` suffixes, all 50 test
definitions and all 12 support functions plus four constants MUST remain equivalent.

#### Scenario: collection identity and test oracles are preserved one-to-one

- **WHEN** the baseline monolith and all three post-partition suites are collected and
  fingerprinted
- **THEN** each side yields exactly 66 unique identical sorted node suffixes, 50 one-to-one
  test definitions and 16 one-to-one helper members
- **AND** executing the three files preserves 55 passed / 11 skipped default semantics and
  55 passed / 11 deselected non-integration semantics without dropped, duplicated, renamed
  or weakened cases
- **AND** `tests/qhh_production_bootstrap_helpers.py` collects no tests, its fixture symbol is
  imported directly at scheduler-owner module scope, and removing that import constructs a
  fixture-not-found RED before the import is restored.

#### Scenario: historical BUG-008 command remains biting

- **WHEN** BUG-008's three-file `-k output_segment_count` command is executed after the
  partition
- **THEN** it passes exactly eight nodes with QHH / registry / production-scheduler
  ownership 5 / 2 / 1
- **AND** the historical QHH path remains a real suite owning all five QHH suffixes rather
  than a compatibility shim or zero-collection placeholder
- **AND** the ledger command and historical evidence remain byte-identical.

#### Scenario: production-owner and helper-only selection reach every partition

- **WHEN** targeted selection runs for a changed model-registry production owner
- **THEN** all three QHH bootstrap partitions remain in the existing owner set alongside
  every prior consumer
- **AND** removing any one of the three edges with a non-same-name probe makes the selector
  contract RED before the edge is restored
- **AND** a helper-only change selects exactly the three module-scope consumers plus the
  existing selector meta rider, and deleting any helper edge makes RED.

#### Scenario: integration ownership remains bound to the real-database lane

- **WHEN** integration markers and the CI `database:` block are evaluated after partitioning
- **THEN** all eleven integration nodes remain together in
  `tests/test_qhh_production_bootstrap_scheduler.py`, while retained/state owners contain
  none
- **AND** the historical database literal is replaced by the scheduler owner and the
  DB-support helper is a second exact database trigger, while retained/state owners are absent
- **AND** a helper-only diff opens the real-database job rather than merely selecting tests
  that skip without the integration environment
- **AND** deleting either scheduler/helper exact edge leaves no surviving exact or glob
  pattern that matches that path
- **AND** neither chosen filename contains `integration`, so existing integration globs
  cannot rescue either required exact edge
- **AND** frozen-final-SHA node-27 evidence executes all eleven nodes as PASSED in an
  isolated temporary database and cleans it without production DB/display mutation.

#### Scenario: structural guard and current commands match the new layout

- **WHEN** the four Python outputs, guard configuration and current scheduler compatibility
  commands are evaluated
- **THEN** every output is strictly below 1,000 lines, the baseline guard blob retains its
  frozen digest, the guard stays outside the #1948 PR-visible change set, its active
  threshold remains 1,000, no QHH replacement exclusion exists, and an ordinary commit
  passes the wired hook
- **AND** each current focused command names the actual owner and collects at least one
  intended node
- **AND** the structural diff contains no production code, registry split, BUG-008 history,
  archived evidence or #1903 behavior.
