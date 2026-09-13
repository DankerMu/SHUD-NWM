# Issue #2224 fixture review (2026-09-10)

- Branch base: `f33441a2dafd910375aab087f61257327055611c`.
- Shared change: `compressed-chunk-cold-tablespace-tiering`.
- Effective fixture: `high`.
- Reviewer: independent `reviewer` leaf agent, read-only.
- Final verdict: `pass`.

## Review history

The first pass returned `revise` with five P1 fixture findings:

1. mandatory origin identity and the complete production caller inventory were
   not explicit enough to prevent a compatibility overload from falling back to
   the parent hypertable;
2. SQL-text assertions could be mistaken for the required TimescaleDB behavior
   proof;
3. catalog/runtime/census/post-target/runbook selector ownership and the location
   of the real integration oracle were underspecified;
4. task 4.0A did not yet require an executable G0/G1 STOP fence invalidating the
   failed window; and
5. the high-risk interpretation correction was not yet carried through the risk
   packs and Invariant Matrix.

The fixture was revised without changing production code. The same reviewer then
confirmed all five findings closed and returned `pass`.

## Accepted contract

- Production parity requires current durable origin schema/name as mandatory,
  no-default input derived from the resolved `CatalogChunk`, with OID and window
  bound by the owning call.
- Missing/empty identity, allowlisted parent, current compressed sibling, or an
  OID/schema/name/window mismatch fails closed with no parent fallback.
- G1 census; runtime preflight, locked revalidation, recompression, post-commit
  readback and reconciliation; and post-target named-group observation all route
  through the same origin-qualified owner. The probe-private fixture helper is
  unchanged.
- Unit tests own identity, quoting, SQL target and drift/disappearance refusal,
  but cannot self-certify Timescale behavior through string inspection.
- An isolated PostgreSQL 15.2 / TimescaleDB 2.10.2 result-and-plan oracle must use
  large sibling data, prove target sensitivity and sibling independence, compare
  direct versus `ONLY` origin forms, and accept only transparent compressed
  business-row behavior without sibling plan nodes.
- Census `3600000` ms and runtime `3600s` statement ceilings remain finite and
  unchanged. Removing or raising them is not the repair.
- Catalog SQL, runtime, census, post-target and runbook owners directly select
  assertion-bearing tests with removal mutants; local/CI skip or collect-only
  cannot substitute for the node-27 isolated oracle.
- The executable runbook must require #2224 merged, reject the failed `a8db554d`
  window and its absent artifacts, and restart from G0 at a new merged SHA.

## Task and evidence state

- Task 4.0 remains complete.
- Task 4.0A remains incomplete until implementation, review, integration evidence,
  CI and merge finish.
- Tasks 4.1-4.8 remain incomplete.
- The shared OpenSpec change remains active.
- The sanitized G1 NO-GO record is failure evidence, not a live PASS receipt.

Mechanical gates before and after review: target OpenSpec strict validation,
evidence placeholder/sensitive-material checks, task-state assertions and diff
hygiene all passed.
