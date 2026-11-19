# continuous-daemon-live-operation Specification

## Purpose

How the continuously running scheduler holds its single-writer lease: renewal by heartbeat, and reclaim of a stale lease only through a compare-and-swap that refuses when the lease changed underneath it.

## Requirements

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
