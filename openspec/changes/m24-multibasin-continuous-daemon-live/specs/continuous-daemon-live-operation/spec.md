## ADDED Requirements

### Requirement: Live receipt proves the m20/m23 chain ran via the generic scheduler
m24 SHALL produce a node-22 live receipt proving the m20/m23 chain (download → canonical → forcing
→ SHUD → parse → frequency → publish) was executed by the `services/orchestrator` scheduler/chain
daemon — not diagnostic scripts — closing m23 Task 5.5/6.6 and m20's 0/33 live gap. (The chain and
BLOCKED-dependency behavior are defined by m20/m23; m24 adds only the live proof of generic-path
execution.)

#### Scenario: Receipt binds daemon-mode generic execution
- **WHEN** a daemon pass completes a fresh cycle on node-22
- **THEN** the receipt records daemon mode, the generic scheduler command, the run identity tuple,
  the gateway submission receipt, warm-start quality, and published manifest/log URIs
- **AND** it evidences that `run_qhh_cycle.sh`/`run_qhh_continuous.py` were not invoked.

#### Scenario: Live dependency blocked
- **WHEN** a required live dependency is unavailable
- **THEN** the receipt reports `BLOCKED` with the exact dependency and evidence path
- **AND** it does not claim business automation or production readiness passed.

### Requirement: Daemon lease has heartbeat and is proven on real NFS
The daemon lease that prevents duplicate passes SHALL use heartbeat/renewal and reconcile owner
liveness before stale reclaim, proven on the node-22 shared `/scratch` filesystem with two
independent processes.

#### Scenario: TTL expiry during a long pass does not double-submit
- **WHEN** two independent processes contend for the lease on real `/scratch`, with the lease TTL
  shorter than the pass duration
- **THEN** the holder renews via heartbeat; the lease carries a token/generation (`lease_token`,
  `heartbeat_seq`/mtime, owner pid-start or boot id), and a contender reclaims a stale lease only
  via compare-and-swap on that token after reconciling host/pid, candidate reservation, and
  `sacct`/`squeue` — if the token/heartbeat changed (holder just renewed), it must not unlink
- **AND** the receipt shows a heartbeat crossing the TTL with the contender not reclaiming; no
  candidate is double-submitted; the proof records the real filesystem type.

#### Scenario: Safe disable is live-proven (delta on m23 contract)
- **WHEN** an operator disables the daemon (the safe-disable contract is m23
  `compute-scheduler-operationalization`)
- **THEN** the m24 receipt references that contract and additionally records the disable command,
  pass id, `disabled_at`, last submit timestamp, and evidence that no submit occurred after disable
- **AND** in-flight run evidence remains queryable.
