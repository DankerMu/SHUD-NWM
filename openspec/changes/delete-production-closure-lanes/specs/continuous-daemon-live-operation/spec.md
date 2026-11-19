## REMOVED Requirements

### Requirement: Live receipt proves the m20/m23 chain ran via the generic scheduler

**Reason**: The m24 live receipt (`services/m24_live/`, `scripts/m24_emit_baseline.py`) is deleted (#3221): the m24 chain has no caller outside its own tests and is no longer an acceptance mechanism.
**Migration**: None. Generic-scheduler execution is the only production path (`nhms-compute-scheduler.service`) and is observed through scheduler pass evidence.

### Requirement: Daemon lease has heartbeat and is proven on real NFS

**Reason**: Replaced by "Daemon lease renews by heartbeat and reclaims a stale lease only by compare-and-swap": the real-NFS proof (`scripts/m24_lease_nfs_proof.py`) and the m24 receipt clauses are cut (#3221), and OpenSpec refuses a MODIFIED block that drops a scenario, so the surviving text moves to a new header.
**Migration**: See the ADDED requirement; the lease behaviour itself is unchanged and stays pinned by `tests/test_production_scheduler.py`.

## ADDED Requirements

### Requirement: Daemon lease renews by heartbeat and reclaims a stale lease only by compare-and-swap

The daemon lease that prevents duplicate passes SHALL use heartbeat/renewal and reconcile owner
liveness before stale reclaim.

#### Scenario: TTL expiry during a long pass does not double-submit
- **WHEN** two processes contend for the lease, with the lease TTL shorter than the pass duration
- **THEN** the holder renews via heartbeat; the lease carries a token/generation (`lease_token`,
  `heartbeat_seq`/mtime, owner pid-start or boot id), and a contender reclaims a stale lease only
  via compare-and-swap on that token after reconciling host/pid, candidate reservation, and
  `sacct`/`squeue` — if the token/heartbeat changed (holder just renewed), it must not unlink
- **AND** no candidate is double-submitted.
