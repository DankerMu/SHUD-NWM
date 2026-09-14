# node27-maintenance-admission-recovery Specification

## Purpose
TBD - created by archiving change restore-node27-maintenance-admission. Update Purpose after archive.
## Requirements
### Requirement: Explicitly authorized incident-only maintenance recovery

The incident recovery SHALL preserve original runtime, governance pin, formal configuration, historical receipts and fixed lifecycle lock while executing at most one bounded compression catch-up per approval. A failed or refused operation SHALL NOT count as successful recovery.

#### Scenario: Temporary budget exception

- **WHEN** the operator approves the #2349 incident exception and all safety checks pass
- **THEN** a private paired budget may launch only compression with bound1 and a matching finite outer wall, without cold activation or original env changes

#### Scenario: Unsafe or interrupted operation

- **WHEN** source, pin, path, timer, candidate or process safety checks fail, or a catch-up is partial or indeterminate
- **THEN** launch or readiness promotion is refused, evidence is preserved, and no automatic large-write retry or lock deletion occurs

#### Scenario: Separately approved scan strategy comparison

- **WHEN** the operator separately approves one comparison, small-data on/off compression preserves identical rows, and the amended plan passes review
- **THEN** only the private compression session SHALL set `timescaledb.enable_compression_indexscan=off`, with an observed effective setting and unchanged100-minute statement ceiling; global settings, formal env, DSN and cold execution remain untouched

#### Scenario: Immediate scheduling restoration after repair

- **WHEN** the comparison succeeds, owned-copy cleanup preserves protected files, and the original compression service genuinely succeeds
- **THEN** its previously active timer SHALL be restored immediately; another failed comparison SHALL remain paused under the explicit user disposition, without an automatic third attempt

#### Scenario: Explicitly approved extended single-chunk window

- **WHEN** both100-minute attempts have failed, the operator separately approves one six-hour window, and fresh capacity, scheduling, identity and exact-candidate checks pass
- **THEN** one private scan-off attempt SHALL use statement21600000ms, wrapper21900s and matching outer25842s with bound1, retaining memory settings and all production-authority protections
- **AND** success SHALL lead to genuine original-service verification and immediate compression-timer restoration; failure SHALL restore retention scheduling, preserve compression pause and prohibit an automatic fourth attempt

### Requirement: Genuine original-service health precedes fresh admission

Recovery SHALL require fresh semantic success receipts and real success of both original maintenance services before a new prepare. The prepare SHALL preserve target415 and original active/pin authority and SHALL NOT execute T0 or production cutover.

#### Scenario: Fresh successful admission

- **WHEN** bounded catch-up and original maintenance services succeed, protected hashes match, and timer states are restored
- **THEN** prepare uses a new hash-bound input bundle and never-created state and its actual result is recorded

#### Scenario: Cosmetic service success

- **WHEN** the only evidence is reset-failed, stale receipt, refused_lock, or an old preparation result
- **THEN** recovery remains incomplete

