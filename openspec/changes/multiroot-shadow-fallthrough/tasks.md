# Tasks: multiroot-shadow-fallthrough (#1329)

Issue: #1329 (S code / M contract). Branch:
`feat/issue-1329-multiroot-shadow-fallthrough`. Depends on merged
#1325 (PR #1328) and #1330 (PR #1331; runtime line drift accounted).

## Evidence Floor

- `uv run pytest -q tests/test_warm_start_chaining.py
  tests/test_state_manager.py tests/test_shud_runtime.py`
- `uv run ruff check .`
- `openspec validate multiroot-shadow-fallthrough --strict
  --no-interactive`

## Deviations (recorded up front)

1. Issue AC asks to rewrite the archived
   `state-save-source-freshness-gate` design D3 rule 6 / D6 and to
   validate that change — archives are immutable and archived
   changes are not validate targets. The superseded text is quoted
   verbatim in this change's design D1; closure is recorded here and
   on issue #1329; validation target is THIS change.
2. Issue's "受影响面" names `tests/test_state_manager.py` as the
   gate-anchor home — corrected by explorer sweep: the
   token-asserting anchors live in `tests/test_warm_start_chaining.py`
   (`:2561`, `:2587`, `:2787`) plus one composed test in
   `tests/test_shud_runtime.py:6439`; `test_state_manager.py` holds
   only ONE gate-side A8 pin (`:2145`; `:2103` is
   `save_state_snapshot`-level and never enters the gate — round-1
   P3 correction).
3. Issue's line cites are stale (pre-#1330 drift); corrected cites
   on 99cfc47d are used throughout the fixture (loop `:634-639`,
   hard raises `:693-696`/`:700-702`, env-context `:569`,
   runtime `:686`/`:699-704`/`:427`/`:443`).

## 1. Fixture

- [x] 1.1 proposal/design/tasks + spec delta authored (this commit)
- [x] 1.2 Reviewer fixture review (read-only) until clean
  (two-iteration repair bound per workflow contract)
  - Round 3 CLEAN (0 P1, 0 P2, 1 cosmetic P3, reviewer: "does not
    warrant a repair round") — both round-2 repairs verified sound
    and fully propagated (six-locus trigger grep, code-verified
    D4-limit claim, reason mechanism + discriminator verified
    against :608-612/:688/:697-708/:174/:187-196). Cosmetic
    closure applied same-round: D2 heading "three edit sites",
    proposal risk-triage "one implementation point" corrected,
    evidence mapping A1-A5 → A1-A6, What-Changes item 2 names the
    guard anchor. Fixture APPROVED for implementation.
  - Round 2 NOT CLEAN (0 P1, 2 P2, 0 P3) — repaired (iteration
    2/2, the bound; repairs 1/3/4/5 of round 1 verified sound, all
    cites re-verified): P2-1 the guard trigger was stated three
    inequivalent ways (proposal: "manifest recorded non-empty
    hours" — strictly broader, wrongly blocks after
    MANIFEST_INCOMPLETE-class rejections; D1 permission: "every
    earlier root fallback-lane" — strictly narrower, wrongly
    blocks after MANIFEST_MISSING) → canonical trigger everywhere:
    an earlier root FELL THROUGH WITH CHECKPOINTS_UNCAPTURED, and
    only that; spec parenthetical weakened to the guard's actual
    scope; the pre-existing MANIFEST_INCOMPLETE-earlier escape
    recorded as a D4 known limit (master-identical, untouched).
    P2-2 the guard needs the rejection reason but
    _StateSourceRejection discards it after formatting (:611-612)
    → named mechanism: store self.reason (third edit site, tasks
    2.1(b)); fallback-lane discriminator named as
    final_ic-is-not-None (:688 vs :697-708, consumer :174/:187-196);
    loop docstring refresh brought in scope.
  - Round 1 NOT CLEAN (1 P1, 3 P2, 1 P3) — repaired (iteration
    1/2; all load-bearing cites verified accurate by the reviewer):
    P1 the REVERSED both-fail geometry (workspace always-fall-
    through reason + object-store publishable-set failure) flips
    the reported token vs master, undisclosed and unanchored, while
    A3 sat on the zero-differential forward half → proposal delta 4
    discloses; A3 split into (a) forward GREEN pin / (b) reversed
    RED anchor. P2-1 cross-root fallback downgrade escape (config
    drift lets a sibling's end-time IC satisfy a run that requested
    checkpoints) → SECOND ruling: cross-root downgrade guard
    (earlier CHECKPOINTS_UNCAPTURED fall-through blocks later
    fallback-lane publication; all-fallback-lane geometry stays
    publishable), propagated to proposal/design D1+D2/spec delta;
    new anchor A6 with naive-implementation teeth. P2-2 "multi-root
    = --manifest-index lane only" overstated → DB-backed lane
    (load_run_context reads hydro_run.output_uri, :117/:135 via
    :167-168) added to delta 1. P2-3 "rules 1-5 unchanged" false →
    rules 3/4 quoted as superseded, unchanged set narrowed to
    1/2/5. P3 test_state_manager.py:2103 is not a gate pin →
    A5(a)/deviation 2 corrected to :2145 only.
- [x] 1.3 `openspec validate multiroot-shadow-fallthrough --strict
  --no-interactive` green (re-run after every repair round)

## 2. Implementation (implementer subagent)

- [x] 2.1 `packages/common/state_cli.py`, THREE edit sites (design
  D2): (a) `_verify_state_source_root` — the two publishable-set
  verdicts change raise class `StateManagerError` →
  `_StateSourceRejection` (same token constant, same detail text);
  (b) `_StateSourceRejection.__init__` — store `self.reason`
  (signature unchanged); (c) `_admit_state_publish_source` loop —
  cross-root downgrade guard keyed on
  `rejection.reason == STATE_SAVE_SOURCE_CHECKPOINTS_UNCAPTURED`:
  a later FALLBACK-lane verified source (`final_ic is not None`)
  is ineligible, reject with the earlier root's reason;
  checkpoint-lane sources unaffected; loop docstring refreshed to
  the re-scoped definition. Nothing else.
- [x] 2.2 Anchors A1-A7 per design D3 (A5(d)/A7 added in PR-review
  round 1; A1/A2/A3(b) RED on master
  99cfc47d; A3(a) GREEN-both-sides message pin with
  wrong-implementation teeth; A4 existing tests verbatim — zero
  edits; A5(b)/(c) new no-fall-through pins for entry-count
  overflow and unparseable manifest with a healthy sibling; A6
  cross-root downgrade guard — naive fall-through implementation
  must fail it, plus A2 companion liveness pin)
- [x] 2.3 Evidence floor + ruff green; deviations reported
  explicitly ("no deviations" stated if none)

## 3. PR

- [x] 3.1 Commit + push; PR with 变更摘要 / 偏离记录 / 测试证据 /
  Evidence-Floor 声明 (PR #1333, head 54abaf39)
- [x] 3.2 CI green (targeted Unit Tests, 54abaf39)

## 4. Review loop

- [ ] 4.1 Cross-review rounds per gate ledger; candidates → dedup →
  per-class verifier batches; findings verified before fix
  - Round 2 focused (577ea1dd, scope = round-1 fix commit): 0 P1,
    0 P2, 4 P3 — both round-1 closures verified with independent
    mutation probes (each mutation killed by exactly 1 test, the
    new anchor); oracle integrity clean (+69/-0). P3s closed
    same-round (#1325 precedent): R2-1 hard-supersede disclosure
    scoped to publishable-set fall-throughs in spec scenario +
    design D1 (pre-existing fall-through reasons: message
    unchanged vs master, which also probed the sibling); R2-2 A7
    marked GREEN-both-sides (mutation-teeth pin, not a master
    differential); R2-3 task 2.2 A1-A7 + task 5.0 ticked with
    #1334; R2-4 A5(d) geometry self-verification pin (workspace
    manifest exists + path-identity discrimination both
    directions) — tests-only implementer edit.
  - Round 1 NOT CLEAN (54abaf39; 2 P2 + 1 P3, all CONFIRMED by two
    independent verifier batches): C1 [P2, spec-disclosure] the
    forward soft-first/hard-later geometry reports the LATER
    root's hard message, falsifying the unqualified rule-5 SHALL
    and delta 4's forward byte-identity claim (verifier probe:
    6/6 combinations diverge vs master; fail-closed intact in all
    12 runs) → fixed direction (a): spec SHALL scoped to
    no-hard-error, scenario extended, proposal delta 4 third
    sub-case, new anchor A5(d). C2 [P2, coverage]
    FINAL_IC_MISSING detail text zero full-string coverage
    (mutation gutting the manifest path survived 394 green; only
    startswith pins exist) → new anchor A7. C3 [P3, coverage,
    DEFER] unsafe-path (:856 zero coverage anywhere) and
    oversized-artifact hard raises unanchored in sibling geometry;
    surgical two-site re-class survives the floor, but the
    plausible regression class (except-widening) is already killed
    by A5(b)/(c) 4-RED; sites untouched by this diff → routed as
    follow-up issue (task 5.0).
- [ ] 4.2 Phase 7 final review clean on final head

## 5. Merge (pre-authorized) and closeout

- [x] 5.0 Follow-ups routed with numbers: #1334 (round-1 C3 DEFER —
  unsafe-path / oversized-artifact hard raises unanchored in
  sibling geometry; `state_cli.py:856` zero coverage anywhere)
- [ ] 5.1 Chinese work summary + evidence posted; CI green on final
  head
- [ ] 5.2 Merge; archive change; loop-log line + audit; close issue
  #1329
