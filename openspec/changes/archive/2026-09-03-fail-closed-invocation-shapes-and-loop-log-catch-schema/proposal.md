# Proposal: fail-closed-invocation-shapes-and-loop-log-catch-schema

Batch change for issues #1691, #1764, #1812, #1662 — one PR, one fixture. All four
are governance/evidence-contract hardening with no runtime behaviour change on the
production data path.

## Why

- **#1691** — `scripts/node27_timeseries_compression_live_evidence.py` requires the
  five `*_invocation` slots to exist but never validates their shape. Because the
  artifact-closure walk only collects exact `{path, sha256, bytes}` mappings, a
  slot written as a four-key mapping, a string, `null`, or a wrapper around a
  valid reference escapes every check and the bundle still qualifies. PR #1690
  pinned that escape as a characterization test; this change closes it.
- **#1764** — `loop_log_audit.rotation_attribution` silently skips any catch
  lacking `round`/`lens`, and `evidence_check --loop-log-entry` never descends
  into `catches`. Re-measured on `docs/review-loop-log.jsonl` @ `f9a1345f`:
  1600 catches, 1576 compliant, **24 non-compliant in 5 entries** (17 `phase`-only
  in lines 440/442/443/445 = PR #1730/#1738/#1746/#1751, plus 7 `lens`-less in
  line 461 = PR #1802 whose line has no `round_lenses` key). ADR 0003's PR #1759 revisit
  records the older 17/1331 figure and must be corrected.
- **#1812** — the still-active change `node22-db-free-scheduler-state` carries an
  ADDED `file-orchestration-journal` delta whose `reconciliation_decision` closed
  enum is six values while code (`ACCEPTED_RECONCILIATION_DECISIONS`) and live
  specs have eight (`identity_mismatch_released`, `operator_verified_absence`
  merged via #1178 and #1755). Archiving as-is would promote a stale enum into
  live spec; `openspec validate` cannot see it.
- **#1662** — the hard-gate false positive on
  `openspec/specs/production-scheduler-orchestration/spec.md:103` was removed by
  the detector fix in #1746. Re-verified on this change's base: test green,
  `hard_gate_status=pass`, `failing_count=0`, spec file untouched. No change is
  made; the issue is closed with the receipt. The optional "mirrored" wording
  question the issue's last comment reserves for a human is left open.

## What changes

1. Verifier input-shape gate for the five `*_invocation` slots (fail closed on any
   non-`{path, sha256, bytes}` value, including wrappers around a valid ref);
   characterization tests flipped to fail-closed assertions; schema descriptions
   and runbook wording updated to the new true statement.
2. Upstream `subagent-workflow` skill (`DankerMu/my-agents`, not this repo):
   `evidence_check --loop-log-entry` validates every catch's `round`/`lens`;
   `loop_log_audit` reports skipped non-compliant catches instead of dropping
   them silently; tests for both; version bump + changelog. Local gitignored
   copies re-synced. Repo side: backfill the 24 catches from PR evidence (or
   declare a line unattributable in the ADR with its line number), correct ADR
   0003's PR #1759 revisit, re-record the corrected ratio.
3. Refresh the `file-orchestration-journal` delta of `node22-db-free-scheduler-state`
   (enum → eight values, retry-permission clause covers both absence decisions),
   validate strict, archive-simulate in a scratch copy.
4. Record the #1662 receipt; no diff.

## Capabilities

- `hypertable-compression` (MODIFIED: the surviving `*_invocation` slots requirement)

## Non-goals

- Whether the five slots stay required (#1398 decided: yes).
- Changing `artifact_references`' three-key criterion in `packages/common/evidence_io.py`.
- A JSON Schema for the whole input bundle (second truth source; rejected in #1691).
- Any runtime/implementation code for #1812; archiving `node22-db-free-scheduler-state`
  (task 9.6 still open).
- ADR 0003's other attribution artefacts (Phase 7 counted as rotated, round-role
  modelling) — explicitly out of #1764's scope.
- Any edit to `openspec/specs/production-scheduler-orchestration/spec.md` (#1662).
- Pushing the vault commit; node-27/node-22 live runs (no production path touched).

Fixture level: expanded (schema/evidence contract, `path`/`schema` triggers).
Repair intensity: high (evidence-chain verifier) — Invariant Matrix in `design.md`.
