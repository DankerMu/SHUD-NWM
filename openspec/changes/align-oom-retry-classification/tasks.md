# Tasks: align-oom-retry-classification

## Risk Triage

```text
Issue type: bugfix (spec-code drift, MUST NOT clause)
Project profile: NHMS/NWM (openspec/project-profile.md)
Blast radius: medium (retry semantics — auto-retry eligibility flips for
  one error code across DB and db-free paths)
Fixture level: expanded
Upstream suggested level: absent (issue is Readiness: needs-triage on the
  ruling only; triaged here — mandatory expanded trigger applies: retry
  semantics)
Why:
- Behavior change on the retry decision surface: OOM jobs stop
  self-healing by design; wrong delivery direction would silently
  disable recovery for genuinely transient siblings (guarded by the
  surface-disposition table D2 and the recompute-boundary anchor)
- Five surfaces (the issue's four sibling copies + the P1-1-discovered
  downstream-resume guard, which needs a production edit); one
  (recompute set) is ruled a non-copy — the boundary needs a two-sided
  test anchor
  (planning-time snapshot — round-1 review expanded to SEVEN surfaces:
  2 guard blocks, 2 justified keeps; see deviation 7 and D2)
Selected risk packs:
- Retry/backoff semantics (auto vs manual, permanent-failure marking)
- Sibling-copy consistency (5 surfaces: 3 edits, 1 justified keep,
  1 guard block; final: 7 surfaces per D2)
OpenSpec change: align-oom-retry-classification
Evidence floor:
- uv run pytest -q tests/test_retry.py tests/test_real_slurm_gateway.py tests/test_production_scheduler.py tests/test_orchestration_chain.py tests/test_file_orchestration_journal.py tests/test_production_slurm_validation.py
  (known pre-existing local red: test_db_free_slurm_storage_root_check_...
  macOS symlink-loop env issue — disclose, don't chase)
- uv run ruff check .
- openspec validate align-oom-retry-classification --strict --no-interactive
- Dual sweep table in PR body (every hit → D2 row or non-goals):
  grep -rn "OUT_OF_MEMORY" AND grep -rn "is_transient_error\|is_retryable_failure\|classify_failure\|TRANSIENT_ERROR_CODES\|TRANSIENT_RETRY_REASON_CODES"
```

## Recorded deviations from issue #1161

1. **Ruling made in-fixture** (AC1 allows design.md): direction A with
   grounds in proposal.md/D1 — following the #1289 precedent of
   recording the semantic ruling in the fixture rather than a separate
   maintainer comment; the merge itself is the maintainer act.
2. **Evidence floor adds `tests/test_orchestration_chain.py`** beyond
   the issue's three-file pytest command: it hosts the OOM state-mapping
   fixtures the issue claims unaffected (currently `:6598,:6634`; the
   issue's `:6278,:6314` were `3d6d3b92`-era lines) — running it is
   the proof of that claim, not scope creep.
3. **`_MISSING_FORECAST_OUTPUT_RECOMPUTE_CODES` keeps OOM** via AC3's
   justified-non-change branch (design D2 row 4, two-sided anchor
   D4 "D2-row-4").
4. **New classifier label `resource_configuration`** — a vocabulary
   addition beyond pure "alignment" (fixture review noted it as an
   undisclosed-deviation candidate); grounds in D2 row 2: no production
   consumer branches on `transient_slurm_runtime` specifically, and
   letting OOM fall to `unknown_failure` would misstate a
   spec-classified code. (Round-1 correction: the earlier generalized
   wording "no production consumer branches on classifier strings" was
   false — `policy_blocked` branches at `scheduler_state_failure.py:202`
   pre-existing, and this change's own D2 rows 5-6 refusal sets consume
   `resource_configuration`.)
5. **Main spec gains one normative scenario** (parity anchor) the issue
   did not ask for — it is both the AC4 anti-drift lock and what gives
   this OpenSpec change a validatable delta.
6. **Production diff includes `scheduler_state_failure.py`** (one guard
   edit, D2 row 5) — the issue's four-surface list missed the
   downstream-resume channel entirely; without this edit the MUST NOT
   clause stays violated in the durable-output geometry (fixture review
   P1-1).
7. **Round-1 review adds a seventh surface and a second production
   edit** — `_model_package_refresh_retry_evidence` gains the OOM
   refusal (D2 row 6): the permanent-only channel became newly
   reachable for OOM precisely BECAUSE this change made OOM permanent,
   and the decision ladder consults it before the permanent guard
   (round-1 finding A1, P1, verifier-confirmed end-to-end with a real
   `run_once()` submission). The five-surface list was itself
   incomplete; the AC2 grep sweep structurally could not see this
   channel (no literal, classification reached via
   `_failure_policy_payload`) — sweep lesson recorded in D2.
8. **Round-1 fix commit extends `.large-file-guard.json` exclude** with
   `services/orchestrator/scheduler_state_failure.py` and
   `tests/test_retry.py` — a permanent commit-ratchet exemption,
   recorded here per the guard-delta precedent
   (`openspec/changes/tier-node27-timeseries-storage/design.md`
   documented exceptions; `docs/review-loop-log.jsonl`
   `large_file_guard_delta` entries). Both files exceeded the
   1000-line threshold at merge base already (1127/2056 lines); this
   PR's marginal edits did not cross the ratchet.

## 1. Fixture

- [x] 1.1 Author proposal/design/tasks + spec delta (one added scenario;
  classification lists unchanged — spec was already correct); ruling
  recorded (D1)
- [x] 1.2 Reviewer fixture review (read-only): round 1 NOT CLEAN
  (1 P1 — the P1-1 fifth surface, measured live; 4 P2, 3 P3), all fixed
  in-fixture; round 2 CLEAN (all 8 fixes verified by in-memory
  simulation incl. the row-5 guard: OOM → blocked/
  permanent_failure_guard, PARSE_FAILED channel untouched, zero
  collateral across 6 test modules) + 6 P3 doc-consistency items fixed;
  `openspec validate align-oom-retry-classification --strict
  --no-interactive` green

## 2. Implementation (implementer subagent)

- [x] 2.1 `retry.py`: set membership move (TRANSIENT → NON_TRANSIENT);
  `failure_classifier` gains `resource_configuration` branch for OOM
- [x] 2.2 `scheduler_state_types.py`: drop OOM from
  `TRANSIENT_RETRY_REASON_CODES`
- [x] 2.3 `scheduler_state_failure.py` `_downstream_failure_restartable`:
  refusal sets gain `OUT_OF_MEMORY` + `resource_configuration` (D2 row 5)
- [x] 2.4 Confirm-and-leave: `_MISSING_FORECAST_OUTPUT_RECOMPUTE_CODES`
  keeps OOM (D2 row 4); `scheduler_state_compat` re-export untouched;
  Slurm raw-state mapping surfaces untouched (non-goals list);
  `slurm_validation.py:1721` evidence flip disclosed, no edit
- [x] 2.5 Rewrite the 5 pinning tests to spec behavior (D4.1-D4.5;
  anchor the two production_scheduler targets by test NAME)
- [x] 2.6 New parity anchor test (spec-text ↔ code sets, bullet-token
  parsing per D4) — red pre-change
- [x] 2.7 New D2-row-4 boundary anchor: half 1 (recompute preserved WITH
  missing-output geometry) green pre- and post-change; half 2
  (manual_retry_required WITHOUT that geometry) RED pre-change — both
  halves' pre-change outputs stated in the brief
- [x] 2.8 New D2-row-5 anchor: OOM + durable output present → NOT
  retry_downstream, no automatic_retry_allowed:True — red pre-change
- [x] 2.9 Red-proof protocol: rewritten tests + parity + row-5 + row-4
  half 2 red against pre-change source, red output recorded in the brief
- [x] 2.10 Round-1 fix pass (D2 rows 6-7, D4 additions):
  `_model_package_refresh_retry_evidence` gains the OOM refusal pair
  after its permanent-only gate (row 6); two-sided row-6 anchor (half 1
  RED against pre-fix head `949697c1` — measured
  `retry_after_model_package_refresh / auto=True`; half 2
  INVALID_MANIFEST channel-preservation green both sides); parity-anchor
  window additionally breaks on heading lines (C2 — mutation-proven:
  reformat+drift fails hardened anchor, passed the un-hardened one);
  floor 1977 passed + 1 disclosed pre-existing red

## 3. Verification (orchestrator)

- [x] 3.1 Evidence floor commands green (at head `949697c1`; post-fix
  floor is 2.10's 1977) — six-file pytest 1975 passed +
  the 1 disclosed pre-existing macOS env failure
  (`test_db_free_slurm_storage_root_check_masks_symlink_loop_path`,
  reproduced identically on the pristine f33de396 extraction); ruff
  clean; validate green; orchestrator spot re-ran retry+gateway files
  (290 passed) and the 5 new/rewritten scheduler anchors (9 passed)
- [x] 3.2 Dual full-repo sweep done (implementer report → PR body):
  every literal hit → D2 row / non-goals / test disposition; every
  indirect consumer (`is_transient_error`/`classify_failure`/set names)
  → D2 rows 3-5, D3 planes, or import-only
- [x] 3.3 Production diff verified confined: `retry.py` (set move +
  classifier branch), `scheduler_state_types.py` (one deletion),
  `scheduler_state_failure.py` (row-5 guard at head `949697c1`; round-1
  fix adds the row-6 refusal pair); elsewhere tests plus one
  `.large-file-guard.json` exclude extension (deviation 8)

## 4. Review loop

- [x] 4.1 Cross-review rounds per gate ledger: round 1 (949697c1)
  not-clean — 3 lenses, 11 candidates, 4 verifier batches, 10 CONFIRMED
  (highest P1: the row-6 sixth surface) / 1 REFUTED, fix pass delivered;
  round 2 (553d2e6a) clean — fullscope + test-evidence CLEAN on all
  substance, P2-only record repairs verifier-confirmed and landed
  inline (5b54b34d)
- [x] 4.2 Phase 7 final review CLEAN on 5b54b34d (seven gates: docs-only
  delta, diff confinement, D2 decisive claims incl. full-ladder D3
  completeness walk, floor 1977+1 reproduced, oracle integrity, seven
  ACs, archive-fold simulation; this closeout-ticks commit trails it
  docs-only, recorded)

## 5. Merge (pre-authorized) and closeout

- [x] 5.0 Follow-ups routed and numbers recorded here — **(a) → #1314;
  (b)(c)(e) → #1313 (one root cause: pre-guard ladder channels
  overwrite permanence); (d) → #1312** — details: (a)
  `auto_retry_skipped` pipeline_event payload (`spec.md:154,170`)
  implemented for NO non-transient code repo-wide (issue #1161 flagged
  it out-of-scope); (b) the downstream-resume channel
  (`_downstream_failure_restartable`) still resumes unknown-default
  non-transient codes (e.g. `PARSE_FAILED`) against the spec's
  unknown-defaults clause — pre-existing, broader than this one-code
  alignment (fixture review P1-1 corollary); (c) the raw-manifest
  repair channels (`:665-718`, `:720-783`) resume ALL permanent codes
  generically before the permanent guard — pre-existing, D2 row 7
  keeps them with recorded tension (round-1 A2 code half); (d)
  spec `:153` "mark permanently failed immediately" is unmet for
  master-row OOM on the file-journal plane: the dormant branch
  `file_orchestration_journal.py:6567-6568` AND its sole production
  caller `chain_forecast_orchestrator_cycle.py:190-197` both return
  without marking — labeling-only blast radius (next cycle blocks via
  the permanent guard), OOM-newly-load-bearing (round-1 D2 + adjacent
  point); (e) candidate-state top-level `retryable: True` bypasses the
  rows-5/6 refusals via `_failure_policy_payload:98-100` — no
  production writer emits the key today, but it is a
  design-acknowledged state key (`scheduler_state_identity_filter.py:
  181,596`) and would open all five refusal codes at once (round-1 D1)
- [ ] 5.1 Chinese work summary + evidence posted; CI green on final head
- [ ] 5.2 Merge; archive change (delta folds into main spec); loop-log
  line + audit; close issue #1161
