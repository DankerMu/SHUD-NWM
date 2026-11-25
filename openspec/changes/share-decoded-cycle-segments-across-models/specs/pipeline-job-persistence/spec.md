## ADDED Requirements

### Requirement: Model-scoped cycle reads SHALL share a cycle's decoded segment records only under the reading call's own fingerprint

A cycle read outside a write window MAY take the decoded records of the
cycle's journal and pipeline-event segments from a bounded per-repository
cache instead of decoding them again, and SHALL do so only when the entry was
stored under a fingerprint equal to the one the same call computed. A call
whose fingerprint observed a containment fault SHALL neither take nor store
an entry. The write-window owner SHALL neither take nor store an entry. A
segment directory whose decode raises SHALL leave no entry for that
directory. Every model's read SHALL still apply the records itself, so its
rows and its errors equal those of a repository that shares nothing.

#### Scenario: one decode per cycle and surface

- **WHEN** several models of one cycle are read while the cycle's files do
  not change
- **THEN** each segment directory of the cycle is decoded once, every read
  computes its fingerprint, and every model's rows equal a cold repository's

#### Scenario: a changed file is decoded again

- **WHEN** a record is appended to the cycle's journal between two models'
  reads
- **THEN** the second read decodes the segments again and its rows contain
  the record

#### Scenario: a containment fault is never served from the shared records

- **WHEN** a segment directory is replaced by a symlink after an entry was
  stored
- **THEN** the next read raises what a cold repository raises, and no entry
  is stored under the fault

#### Scenario: the window owner reads for itself

- **WHEN** a thread reads models of a cycle inside that cycle's write window
- **THEN** it decodes the segments on each read and the shared cache is
  left as it was

#### Scenario: an unreadable cycle fails for every model

- **WHEN** a cycle log has a segment gap, a segment past the limit, or a
  malformed line
- **THEN** every model's read raises the same error and no entry exists for
  the failing segment directory

#### Scenario: the shared records are not modified by scoring

- **WHEN** completion scoring runs over a cycle whose records are shared
- **THEN** the shared records are, afterwards, deep-equal to what was decoded
