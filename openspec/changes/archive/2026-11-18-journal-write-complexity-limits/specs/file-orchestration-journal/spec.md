## ADDED Requirements

### Requirement: Journal writes SHALL NOT persist a record the reader would refuse for complexity

Every file-journal write lane SHALL check the record against the repository's own read limits (`max_json_depth`,
`max_json_nodes`) before its first byte:
- **Depth:** the depth limit SHALL leave a fixed per-record-type headroom for the extra levels the latest view adds
  around that record type.
- **Nodes:** the node limit SHALL apply to the record itself.
- **Refusal:** an over-limit write SHALL be refused with `file_journal_json_depth_exceeded` or
  `file_journal_json_node_limit_exceeded` and SHALL leave the journal tree byte-identical.
- **Batches and historical import:** a batch or historical import containing such a record SHALL be refused whole.
- **Out of scope:** latest-view node counts, which include the view's wrapper and the cycle's other rows, remain a
  read-side limit. Restore lanes that rewrite existing journal content are not checked.

#### Scenario: A record at the depth limit round-trips through every reader

- **WHEN** a record at the deepest admitted depth for its record type is written
- **THEN** a fresh repository instance reads its journal line, its direct row and its latest view without a complexity
  error

#### Scenario: A record one level deeper is refused with zero bytes

- **WHEN** a record one level deeper than admitted for its record type is written through any journal write lane
- **THEN** the write raises `file_journal_json_depth_exceeded` before any byte, and every file under the journal root is
  unchanged

#### Scenario: A record over the node limit is refused with zero bytes

- **WHEN** a record with `max_json_nodes + 1` nodes is written
- **THEN** the write raises `file_journal_json_node_limit_exceeded` before any byte, and a record with exactly
  `max_json_nodes` nodes is admitted and readable from its journal line and direct row

#### Scenario: A historical import holding one over-limit record writes nothing

- **WHEN** a historical scheduler-state import contains one record over the limit
- **THEN** the import is refused before its first append, and the journal tree is unchanged
