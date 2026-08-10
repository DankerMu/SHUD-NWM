# Design: multiroot-shadow-fallthrough (#1329)

Contract source: issue #1329 (implementation-ready; the single open
call — accept the D3 re-scope — is ruled in proposal.md). Explorer
facts (2026-08-10 sweep on master 99cfc47d) cited inline.

## D1. The re-scope and the superseded archived text

New definition: a VERIFIED ROOT passes G2-G5 AND yields a
publishable artifact set (non-empty verified `checkpoints`, or the
gated final-IC fallback with a verified `final_ic` entry, subject
to the cross-root downgrade guard below). The two publishable-set
verdicts become per-root fall-through rejections.

Superseded archived text (recorded verbatim, round-1 P2-3 — THREE
rules are superseded, not one; the archive is NOT edited —
`openspec/changes/archive/2026-08-09-state-save-source-freshness-gate/design.md`):

- D3 rule 3 (`:197-198`): "For each existing root, evaluate G2-G5.
  The FIRST root that passes G2-G5 fully is the verified root;
  later roots are ignored." — superseded by the new verified-root
  definition above.
- D3 rule 4 (`:199-201`): "A root failing G2-G5 falls through to
  the next existing root — EXCEPT the unreadable-manifest case
  (D1: hard reject, no fall-through)." — superseded: the
  fall-through set now additionally contains the two
  publishable-set verdicts; the hard-exception set is unchanged.
- D3 rule 6 (`:204-216`): "Post-verification branch rejects BIND to
  the verified root and never fall through (PR review round-1
  ruling, R1): a root that passes G2-G5 IS the verified root (rule
  3), so `CHECKPOINTS_UNCAPTURED` and `FINAL_IC_MISSING` — decisions
  taken AFTER verification — hard-reject even when a later root
  exists. Consequence accepted as a known limit (D6) …" —
  superseded by the re-scope; its SECOND half (the A8
  legacy hard errors keep verbatim messages and never fall
  through) SURVIVES and is re-pinned here (A5).
- D6 (`:275-281`): "Multi-root shadow … Routed as a follow-up issue
  (fall-through-eligible post-gate tokens would need a D3 re-scope
  of 'verified root')." — this change IS that follow-up; the
  archive's own routing note anticipated exactly this re-scope.

Rules 1, 2 and 5 of archived D3 are unchanged — in particular rule
5: when no root publishes, the reported reason is the FIRST
existing root's rejection (loop exhaustion re-raises
`StateManagerError(str(first_rejection))`, `state_cli.py:640`).
Consequence disclosed in proposal delta 4: in the REVERSED
both-fail geometry (first root fails with an always-fall-through
reason, later root fails on publishable-set) the reported token
changes from master's later-root hard token to the first root's
reason — rule-5 uniformity now applies to every non-hard failure.

CROSS-ROOT DOWNGRADE GUARD (second ruling, proposal; single
canonical trigger — round-2 P2-1): a later root's final-IC
fallback is ineligible when an earlier existing root FELL THROUGH
WITH `CHECKPOINTS_UNCAPTURED` (that rejection proves the run's
configuration requested checkpoint states); the loop rejects with
that earlier root's reason. This preserves the archived
no-downgrade invariant (spec scenario "A total checkpoint miss
cannot downgrade to the fallback") across roots — without it,
config drift between attempts would let a sibling's single
end-time IC silently satisfy a run that requested checkpoints.
Checkpoint-lane publication by a later root is NOT guarded
(checkpoints are never a downgrade), and fallback-lane publication
is allowed UNLESS that specific rejection occurred earlier — the
A2 liveness case (all-fallback-lane roots) and the
`FINAL_IC_MISSING`-then-checkpoint-sibling case both stay
publishable.

## D2. Implementation (three edit sites in state_cli.py)

`_verify_state_source_root` (`state_cli.py:665-708`) already reads
everything the re-scoped verdicts need inline
(`raw_checkpoints` `:685`, `requested_checkpoint_hours` `:689`,
`final_ic` `:697`). The change:

- `:693-696`: `raise StateManagerError(f"{STATE_SAVE_SOURCE_CHECKPOINTS_UNCAPTURED}: …")`
  → `raise _StateSourceRejection(STATE_SAVE_SOURCE_CHECKPOINTS_UNCAPTURED, "<same detail>")`
- `:700-702`: same transform for `STATE_SAVE_SOURCE_FINAL_IC_MISSING`.
- `_StateSourceRejection` (`:608-612`) gains a stored attribute:
  `self.reason = reason` in `__init__` (round-2 P2-2 — the class
  currently discards the token after formatting, so the guard
  would otherwise need a message-prefix match that silently
  couples to formatting; the attribute is the named mechanism.
  `__init__` signature unchanged; THIRD edit site, in scope).
- Cross-root downgrade guard, in `_admit_state_publish_source`'s
  loop (`:634-639`): track whether any earlier existing root's
  `rejection.reason == STATE_SAVE_SOURCE_CHECKPOINTS_UNCAPTURED`;
  when a later root verifies onto the FALLBACK lane
  (discriminator: the returned `_VerifiedStateSource.final_ic` is
  not None — the shape returned at `:697-708`; checkpoint-lane
  sources have `final_ic=None`, `:688`, and the consumer already
  branches on exactly this at `:174`/`:187-196`), do not accept
  it: re-raise the tracked earlier rejection as the loop's outcome
  (`StateManagerError(str(...))`, same shape as `:640`).
  Checkpoint-lane sources are accepted regardless of the flag.
  The loop's docstring (`:620-626`, "the FIRST one passing G2-G5
  wins") is refreshed to the re-scoped definition — in scope.

No public signature changes. Message identity by construction:
`_StateSourceRejection.__init__` (`:611-612`) formats
`f"{reason}: {detail}"` — the exact current inline f-string shape —
and both the loop-exhaustion path (`:640`) and the direct raise
produce the token-leading message the CLI contract pins.

Non-interference pins (explorer facts):
- A8 entry-count overflow raises in BOTH the per-root gate path
  (`_verify_declared_checkpoints`, `:747-751`) and the
  post-selection load path (`_load_state_checkpoint_manifest`,
  `:871-875`); oversized-artifact raises at `:913` reached from
  `:845-850` (gate) and `:250-254` (publish-time normalization).
  All stay `StateManagerError` — hard, no fall-through, verbatim.
- Post-gate empty-list guard (`:179-186`) untouched.
- Root enumeration untouched (`_state_output_roots` `:643-658`;
  workspace `:649`, `output_uri` block `:652-657`).
- No external consumer keys on the two tokens (repo-wide grep:
  tests + spec prose + archives only).

## D3. Anchors (tests/test_warm_start_chaining.py unless noted;
RED on master 99cfc47d unless marked)

- **A1 shadow-checkpoints publishes the sibling** (issue AC-2): two
  roots — workspace total-miss witness (non-empty
  `requested_checkpoint_hours`, empty `checkpoints`), object-store
  (`output_uri`) healthy checkpoint tree ⇒ publish SUCCEEDS and the
  published artifacts are the OBJECT-STORE root's (assert content
  identity, not just success); no `CHECKPOINTS_UNCAPTURED` raise.
  RED on master (hard reject at the first root).
- **A2 shadow-final-ic publishes the sibling** (issue AC-3):
  workspace zero-hours manifest WITHOUT `final_ic` + object-store
  healthy zero-hours tree with verified `final_ic` ⇒ publishes the
  sibling's named IC with `valid_time == run.end_time`; no
  `FINAL_IC_MISSING` raise. RED on master.
- **A3 both-roots-unpublishable determinism** (issue AC-4, rule 5;
  TWO sub-anchors, round-1 P1):
  (a) FORWARD geometry (GREEN-both-sides message pin): workspace
  total-miss + object-store manifest-missing ⇒ reject with the
  WORKSPACE root's `STATE_SAVE_SOURCE_CHECKPOINTS_UNCAPTURED`
  message, token leading, byte-identical to master's text (master
  hard-raises the same string at the first root; differential
  value is against an implementation that reports the LAST root's
  reason or a generic exhaustion message).
  (b) REVERSED geometry (RED on master — the one both-fail shape
  whose token actually changes, proposal delta 4): workspace
  manifest-missing + object-store total-miss ⇒ reject with the
  WORKSPACE root's `STATE_SAVE_SOURCE_MANIFEST_MISSING` reason
  (master surfaced the object-store root's hard
  `CHECKPOINTS_UNCAPTURED`).
- **A4 single-root verbatim pins** (issue AC "不回归"): existing
  anchors pass UNCHANGED — `test_state_save_rejects_total_checkpoint_miss_instead_of_final_ic_downgrade`
  (`:2561`), `test_state_save_rejects_fallback_manifest_without_final_ic_entry`
  (`:2587`), `test_state_save_reports_the_first_existing_roots_reason_when_every_root_fails`
  (`:2787`), and #1330's composition test
  (`tests/test_shud_runtime.py:6439`, `output_uri=None` single-root).
  Zero edits to any existing assertion (oracle integrity).
- **A5 A8 no-fall-through pins** (issue AC-5): (a) oversized
  declared artifact — the existing GATE-side pin stays
  (`tests/test_state_manager.py:2145`,
  `test_state_save_checkpoint_ic_read_is_bounded_before_normalization`;
  round-1 P3 correction: `:2103` is a `save_state_snapshot`-level
  test that never enters the admission gate and is NOT counted as
  an A8 gate pin); (b) NEW pin
  (pre-existing zero coverage, explorer fact): first root's manifest
  with `MAX+1` checkpoint entries + HEALTHY second root ⇒ hard
  `StateManagerError`, no fall-through, message verbatim
  "State checkpoint manifest exceeds maximum entry count: {n} > {max}";
  (c) NEW pin: first root's manifest present-but-unparseable (JSON
  garbage) + healthy second root ⇒ existing
  "Invalid state checkpoint manifest" hard error, no fall-through.
  (b)/(c) are GREEN-both-sides pins on new geometry (master also
  hard-rejects) — their teeth are against an over-eager
  implementation that widens fall-through beyond the two re-scoped
  tokens.

- **A6 cross-root downgrade guard** (second ruling; GREEN-both-sides
  on outcome, differential teeth against the naive fall-through
  implementation this change would otherwise invite): workspace
  total-miss (non-empty requested hours) + object-store HEALTHY
  zero-hours fallback tree (verified `final_ic`) ⇒ publish REJECTS
  with the workspace root's `CHECKPOINTS_UNCAPTURED` message
  (master rejects with the same message via the hard raise; a
  guard-less fall-through implementation would publish the
  sibling's end-time IC — that implementation must fail this
  anchor). Companion liveness pin: A2's all-fallback-lane geometry
  still publishes (the guard keys on an earlier
  `CHECKPOINTS_UNCAPTURED` rejection, not on lane membership
  alone).

## D4. Known limits

- The downgrade guard's trigger is exactly the
  `CHECKPOINTS_UNCAPTURED` rejection (round-2 P2-1 canonical
  wording). An earlier root whose manifest also recorded non-empty
  requested hours but which falls through BEFORE that verdict —
  `_verify_declared_checkpoints` runs at `:686`, before
  `requested_hours` is read at `:689`, so `MANIFEST_INCOMPLETE` /
  `ARTIFACT_CHECKSUM_MISMATCH` win the race — does NOT arm the
  guard, and a later fallback-lane root publishes. This escape is
  PRE-EXISTING master behavior (master falls through those
  rejections identically and publishes the later fallback root);
  this change neither opens nor closes it. Recorded, not guarded —
  closing it would require reading requested-hours on every
  rejected root, a contract widening out of scope here.

- The shadow recovery publishes attempt N's OLDER healthy state —
  correct by the admission contract (identity + witness + integrity,
  deliberately not recency). The
  total-miss attempt's own state remains unpublished until a
  successful re-run; this change removes the stall, not the re-run.
- Root ORDER stays workspace-first: a healthy workspace root still
  wins over a healthier object-store root; the re-scope only lets
  unpublishable roots yield.
- The `--run-id` env-context lane stays single-root by construction
  (`output_uri=None`, `:569`) — the shadow geometry is unreachable
  there; no anchor attempts it.
