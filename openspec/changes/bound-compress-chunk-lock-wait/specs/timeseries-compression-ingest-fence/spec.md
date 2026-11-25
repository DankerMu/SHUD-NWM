## MODIFIED Requirements

### Requirement: Contended deferral is distinguishable from failure

The compression receipt and exit code SHALL distinguish chunks deferred because of
contention from chunks that failed. Contention is fence contention, a tick deadline that
leaves no room for a full fence wait, or a relation-lock timeout of the compress statement
(SQLSTATE 55P03) after which the read-only reconcile shows the chunk still uncompressed.

#### Scenario: Only deferrals

- **WHEN** every selected chunk either compressed or was deferred because of contention, and at least one was deferred
- **THEN** the outcome is `deferred`, the run exits 0, and the receipt lists the deferred chunks and their count

#### Scenario: A relation-lock timeout is a deferral

- **WHEN** the compress statement of a chunk ends with a lock timeout and the reconcile shows the chunk uncompressed
- **THEN** the chunk is recorded as `deferred_contended` with `contended_on` equal to `relation_lock`, without an error, and it counts as a deferral for the outcome and the exit code

#### Scenario: A real failure

- **WHEN** a chunk compression fails for a reason other than contention, a statement timeout included
- **THEN** the outcome is `partial` and the run exits non-zero, as before
