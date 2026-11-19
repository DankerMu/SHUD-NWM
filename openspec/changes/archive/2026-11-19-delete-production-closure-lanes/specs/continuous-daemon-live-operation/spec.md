## REMOVED Requirements

### Requirement: Live receipt proves the m20/m23 chain ran via the generic scheduler

**Reason**: The m24 live receipt (`services/m24_live/`, `scripts/m24_emit_baseline.py`) is deleted (#3221): the m24 chain has no caller outside its own tests and is no longer an acceptance mechanism.
**Migration**: None. Generic-scheduler execution is the only production path (`nhms-compute-scheduler.service`) and is observed through scheduler pass evidence.

### Requirement: Daemon lease has heartbeat and is proven on real NFS

**Reason**: Replaced by "Daemon lease renews by heartbeat and reclaims a stale lease only by compare-and-swap": the real-NFS proof (`scripts/m24_lease_nfs_proof.py`) and the m24 receipt clauses are cut (#3221), and OpenSpec refuses a MODIFIED block that drops a scenario, so the surviving text moves to a new header.
**Migration**: See the ADDED requirement; the scenarios are restated from the live lease (`services/orchestrator/scheduler_lease.py`): the original text named `sacct`/`squeue`, candidate-reservation and pid-start/boot-id checks that the lease does not perform. Pinned by `tests/test_production_scheduler.py`.

## ADDED Requirements

### Requirement: Daemon lease renews by heartbeat and reclaims a stale lease only by compare-and-swap

The file lease that prevents duplicate scheduler passes SHALL be renewed by heartbeat while a pass runs and SHALL be
reclaimed only when it is stale, the staleness decision reconciling owner liveness first.

#### Scenario: A renewed lease is never reclaimed
- **WHEN** a pass outlives the lease TTL and its holder keeps renewing
- **THEN** each renewal advances `heartbeat_seq` and the lock mtime, and a contender that finds the lock younger than the
  TTL treats it as held

#### Scenario: Staleness reconciles owner liveness
- **WHEN** a contender finds a lock older than the TTL
- **THEN** on the owner's host it probes the owner pid: a live owner is never reclaimed, a dead owner makes the lock stale
- **AND** when the owner is on another host or cannot be probed, the lock is stale only after twice the TTL of silence
- **AND** a stale lock that is not a scheduler-owned lease (owner, schema version, `lease_token`, pass id) is refused as
  unsafe instead of reclaimed

#### Scenario: Reclaim is a compare-and-swap on the lease token
- **WHEN** a contender is about to reclaim a stale lease
- **THEN** it re-reads the lock and unlinks it only if `lease_token` and `heartbeat_seq` are unchanged; if either changed
  (the holder just renewed), it does not unlink and does not acquire
