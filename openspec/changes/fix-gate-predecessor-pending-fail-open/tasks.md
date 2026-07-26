# Tasks — fix-gate-predecessor-pending-fail-open (#1150)

Fixture level: compact (issue suggested S-M; production gate change in
`services/orchestrator/` + red-provable integration test → compact, not none).

Risk triage: p1 fail-open in a production admission gate. Seams under test:
the `_build_candidates` seam (same seam as T15, upstream-declared in issue
acceptance criteria). Risk packs selected: contract pack (gate/evidence
contract, spec alignment) + test-integrity pack (red-proof discipline,
single-value pins). Not selected: perf/security packs — no hot-path or
trust-boundary change.

## 1. Gate fix (implementer)

- [x] 1.1 In `services/orchestrator/scheduler_generation_gate.py`, split the
  `history_exists=False` branch (currently `:452-455`) by transition
  decision: return `None` (cold-seed passthrough) ONLY when
  `transition.decision == _generation.TransitionDecision.WARM_CONTINUE` —
  positive predicate so any future unenumerated decision fails CLOSED; for
  `block_predecessor_pending` (and anything else), fall through to the existing blocked-evidence
  return directly below (typed reason
  `state_snapshot_index_prior_checkpoint_missing_after_history`). Update the
  NOTE comment (`:445-451` + `:453-454`) so it no longer claims the branch is
  unreachable-for-blocks; keep it short.
- [x] 1.2 No new reason string, no classifier change, no edits to
  `state_manager.py`, no edits to the legacy path `:231-240`.

## 2. Red-provable integration test (implementer)

- [x] 2.1 Add `test_env_override_blocks_predecessor_pending_without_earlier_history`
  to `tests/test_scheduler_generation.py`, cloned from the T15(b) fixture at
  `:1120` with ONE change to the state index: the single current-generation
  entry sits at `valid_time=2026-05-22T00:00:00Z` — strictly LATER than the
  candidate cycle `2026-05-21T12:00:00Z` (so the strictly-earlier probe
  reports `history_exists=False` while the generation-scoped matrix signal
  still sees history → `block_predecessor_pending` reaches the gate
  fallback). Pin the entry's `cycle_id="gfs_2026052112"`, `lead_hours=12`
  (internally consistent with the 2026-05-22T00Z valid_time; verified
  end-to-end in fixture review). Declaration stays valid, in-window,
  `effective=2026-05-21T00Z` (matches the spec scenario's WHEN preconditions;
  the matrix takes the current-generation branch (e) where the declaration is
  not consulted). Do NOT place the entry at `valid_time == candidate cycle`: that
  shape hits `state_snapshot_index_cycle_id_mismatch` earlier (lead_hours
  matches, expected cycle_id `gfs_2026052100` does not; verifier-probed) and
  misses the target branch.
- [x] 2.2 Assertions (env=false leg): `candidates == []`, `len(blocked) == 1`,
  single-value reason pin
  `state_snapshot_index_prior_checkpoint_missing_after_history` (no OR-set,
  no `in {`), `state_evidence["registry_cutover_transition"]["decision"] ==
  "block_predecessor_pending"`, and
  `state_evidence["state_history"]["history_exists"] is False`.
- [x] 2.3 env=true non-regression leg (same fixture, may be same test or a
  sibling): still blocked, reason
  `state_snapshot_index_exact_checkpoint_missing` unchanged.
- [x] 2.4 Red proof: the env=false leg must FAIL on the pre-fix gate for the
  right reason (candidate ADMITTED — `candidates` non-empty), captured by
  running the new test with task 1's edit reverted (e.g. `git stash` of the
  gate hunk or scratch checkout). Record the red output in the report; the
  orchestrator carries it into the PR description (issue AC).

## 3. Verification (orchestrator)

- [x] 3.1 `uv run pytest -q tests/test_scheduler_generation.py tests/test_production_scheduler.py tests/test_scheduler_backfill_predecessor.py tests/test_state_manager_generation_history.py` all green.
- [x] 3.2 `uv run ruff check .` clean.
- [x] 3.3 `openspec validate fix-gate-predecessor-pending-fail-open --strict --no-interactive` valid.
- [x] 3.4 Existing guards stay green untouched: T15(b) `:1120`
  (history-exists branch), T15(c), stale-declaration test, D8.9 preflight
  cases (T17 section, `:1721+`).

## 4. Spec + PR bookkeeping (orchestrator)

- [x] 4.1 MODIFIED requirement delta in
  `specs/file-state-snapshot-index/spec.md` (this fixture): heading
  byte-identical, both existing scenarios preserved verbatim, +1 scenario for
  the no-earlier-history shape.
- [x] 4.2 PR body records the disposition of the legacy sibling
  `scheduler_generation_gate.py:239-240` (untouched: no transition decision
  on that path, cold-seed is design intent) — issue AC requires this.

## Evidence mapping (issue AC → tasks)

- AC red-provable test → 2.1/2.2/2.4
- AC env=true non-regression → 2.3
- AC warm_continue non-regression → holds by construction: the added
  predicate conjunct is a no-op on the WARM_CONTINUE path, and
  `warm_continue` + `state_snapshot_index_exact_checkpoint_missing` is
  unreachable with the real provider (both sides derive the identity key via
  `_state_index_identity_key` with identical inputs; verifier-proven, incl.
  no-TOCTOU via the memoized snapshot provider). 3.4's guards cover adjacent
  behavior, not this branch.
- AC four-suite green + ruff → 3.1/3.2
- AC legacy-sibling disposition in PR → 4.2
