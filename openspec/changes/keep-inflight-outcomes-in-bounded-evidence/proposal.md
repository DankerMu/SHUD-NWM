# Keep both reconcile lanes' outcomes in the bounded evidence fallback

## Why

`_compact_bounded_restart_reconcile`
(`services/orchestrator/scheduler_evidence_payload.py:298-313`) rebuilds only the
`reserved_unbound` lane. The `inflight` lane is never mentioned in the function
body, so on every bounded (over-size) pass the whole `restart_reconcile.inflight`
key disappears from the artifact.

The asymmetry is not a deliberate trade-off. The constant directly above
(`:37-39`) keeps **both** lanes' failure keys and its comment states the intent
plainly:

> Both reconcile segments record their own failure key ... and either can be the
> only one present, so the compact block must keep both.

So `inflight_error` survives while `inflight.outcomes` is deleted.

**The deleted lane is the one that matters most.** `identity_mismatch_blocked` is
written by `reconcile_inflight_jobs` (`reconcile.py:1076-1087`) and therefore lands
in `inflight`. Meanwhile `_BOUNDED_RESTART_RECONCILE_OUTCOME_KEYS` (`:41-51`)
deliberately preserves `identity_blocked_streak` with the comment *"the no-progress
counter is the whole point of the compact block under evidence pressure; dropping it
would hide the wedge it exists to expose"* — a protection that only ever applies to
`reserved_unbound`. The signal the guard set out to protect is deleted on the lane
where it actually appears.

Measured on node-22 (2026-08-23): 4 of 6 consecutive passes took the bounded path,
i.e. roughly two thirds of passes carry no inflight record at all.

This is **non-conformance with the archived spec**, not a behaviour change. The
requirement's THEN names both lanes' error keys and does not restrict "per-outcome
summary rows" to one lane. But it does not name the lanes for the outcome rows
either — and that silence is exactly what let a half-implementation read as
conformant, so this change also closes the ambiguity.

Cost of the fix is negligible: the whole `restart_reconcile` block measures 6,447 B
in an un-truncated sample whose `skipped_candidates` is 1,244,635 B.

## What Changes

- `_compact_bounded_restart_reconcile` treats `inflight` and `reserved_unbound`
  symmetrically: each lane present in the source keeps an `outcomes` list whose rows
  are filtered through `_BOUNDED_RESTART_RECONCILE_OUTCOME_KEYS`; a lane absent from
  the source stays absent from the fallback.
- The spec scenario names both lanes explicitly.

## What does not change

- The size bound, the fail-closed terminal compaction tier, and the top-level
  `resource_limit_blocked` / `limit.reason` contract.
- The outcome key set itself.
- Within-limit artifacts, which never take this path.
- Candidate-list degradation tiers and the `limit.candidate_lists` marker.

## Out of Scope

- The drop **ordering** across evidence sections (candidate lists remain the bulk).
- Why a normal pass exceeds the bound at all — `skipped_candidates` and `retention`
  dominate the payload. Reported separately, not addressed here.
- Adding a reason class to the identity gate (#1795).
