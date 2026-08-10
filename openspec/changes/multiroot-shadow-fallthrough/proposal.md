# Proposal: Multi-root shadow fall-through (verified-root re-scope)

## Why

Issue #1329 — the follow-up pre-registered in the archived
`state-save-source-freshness-gate` design D6: attempt N+1's
total-miss workspace tree (witness present, `checkpoints` empty,
`requested_checkpoint_hours` non-empty) shadows attempt N's healthy
object-store tree. `_admit_state_publish_source` treats
`_StateSourceRejection` as the fall-through signal
(`packages/common/state_cli.py:634-639`), but the two
post-verification verdicts `STATE_SAVE_SOURCE_CHECKPOINTS_UNCAPTURED`
(`:693-696`) and `STATE_SAVE_SOURCE_FINAL_IC_MISSING` (`:700-702`)
raise `StateManagerError` directly, escaping the loop — the healthy
sibling root is never probed. Recoverable liveness cost, not a
correctness defect (fail-closed direction), but the warm-start chain
stalls where it could self-heal, and the geometry is routine: the
failure lane uploads no artifacts (`runtime.py:443`), so
"newer-but-failed workspace / older-but-healthy object-store" is the
normal retry shape (`runtime.py:686` writes the witness before the
`STATE_CHECKPOINTS_MISSING` raise at `:699-704`).

## Ruling: accept the D3 contract re-scope (the issue's open call)

"Verified root" is re-scoped from "passes G2-G5" to "passes G2-G5
AND yields a publishable artifact set" — the issue's own recommended
path, adopted as this fixture's ruling. The two tokens become
per-root fall-through verdicts (`_StateSourceRejection`); rule 5's
determinism ("no root publishes → report the FIRST existing root's
reason") is unchanged. The alternative (keep the hard reject, add a
diagnostic hint) is rejected: it preserves the liveness cost and
moves the recovery decision to a human.

SECOND ruling (fixture round-1 P2-1 — cross-root downgrade guard;
trigger wording canonicalized in round 2, P2-1): the final-IC
fallback lane on ANY root is ineligible whenever an EARLIER
existing root FELL THROUGH WITH `CHECKPOINTS_UNCAPTURED` — that
rejection is the proof that the run's configuration requested
checkpoint states; in that case the publish rejects with the
earlier root's reason. Rationale: publishing a sibling attempt's
single end-time IC in place of requested checkpoint states is
exactly the downgrade the archived rule forbade within one witness
— config drift between attempts (a geometry the archive itself
records) must not re-open it cross-root. The guard keys on the
`CHECKPOINTS_UNCAPTURED` rejection ONLY: an earlier root that
fails before its requested hours are read (`MANIFEST_MISSING`,
`MANIFEST_INCOMPLETE`, checksum mismatch, …) does not arm it —
fallback publication past such roots is pre-existing master
behavior, unchanged here (design D4 known limit). Cross-root
fallback therefore remains available in the A2 liveness case.

## What Changes

1. `packages/common/state_cli.py`: (a) the two raises at `:693-696`
   and `:700-702` change class from `StateManagerError` to
   `_StateSourceRejection` (same token, same detail text) —
   `_StateSourceRejection` formats to `f"{reason}: {detail}"`
   (`:611-612`) and the loop's exhaustion path re-raises
   `StateManagerError(str(first_rejection))` (`:640`), so the
   single-root message is byte-identical BY CONSTRUCTION; (b) the
   multi-root loop in `_admit_state_publish_source` gains the
   cross-root downgrade guard (second ruling): when an earlier
   existing root fell through with `CHECKPOINTS_UNCAPTURED`, a
   later root that verifies onto the final-IC fallback lane is NOT
   accepted — the loop rejects with the earlier root's reason
   (design D2).
2. Anchors per design D3 (shadow publish both lanes; both-roots-fail
   determinism both geometries; single-root verbatim pins; A8
   no-fall-through pins incl. the previously untested entry-count
   overflow; A6 cross-root downgrade guard).
3. Spec delta: MODIFIED requirement "Publish-side state admission is
   fail-closed" (verified-root definition + shadow scenario).
4. Archived `state-save-source-freshness-gate` D3 rule 6 / D6 are NOT
   edited (archives immutable); the re-scope is recorded here (design
   D1 quotes the superseded text) and on issue #1329.

## Behavior deltas (disclosed)

1. Multi-root geometry (round-1 P2-2 correction — BOTH multi-root
   lanes, not only db-free): the `--manifest-index` lane wires
   `output_uri` from the candidate manifest
   (`scheduler_candidate_manifest.py:220`), and the DB-backed lane
   reads it from `hydro.hydro_run.output_uri`
   (`StateRunRepository.load_run_context`, `state_cli.py:117`/`:135`,
   reached via `save_state_for_run` `:167-168`). In either lane a
   workspace root failing ONLY on publishable-set now falls
   through; a healthy sibling publishes where master hard-rejected.
   The env-context `--run-id` lane is structurally single-root
   (`output_uri=None`, `state_cli.py:569`) — no change.
2. Single-root geometry: token, message text, CLI exit — all
   byte-identical (by construction, pinned A4).
3. No external consumer breaks: repo-wide grep shows the two tokens
   reach only tests, the live spec prose, and archived docs; the
   orchestrator's array classifier records a generic stage failure
   regardless of token (archived D4).
4. Both-roots-unpublishable REVERSED geometry changes the reported
   token (round-1 P1 disclosure): when the FIRST existing root
   fails with an always-fall-through reason (e.g.
   `MANIFEST_MISSING`) and a LATER root fails with a
   publishable-set reason, master surfaced the later root's hard
   `CHECKPOINTS_UNCAPTURED`/`FINAL_IC_MISSING`; the re-scoped gate
   reports the FIRST root's reason (rule-5 uniformity). The
   forward geometry (first root publishable-set-fails, later root
   also fails SOFTLY) keeps master's message byte-identical.
   Anchored both ways (A3(a) forward GREEN pin, A3(b) reversed
   RED). THIRD sub-case (PR-review round-1 C1 disclosure): when
   the first root falls through on publishable-set and a LATER
   root raises a HARD error (unparseable manifest, unsafe declared
   path, oversized artifact, entry-count overflow), the hard error
   escapes the loop and is the reported message — master surfaced
   the first root's `CHECKPOINTS_UNCAPTURED`/`FINAL_IC_MISSING`
   without ever opening the sibling, so these messages change;
   the loudest actionable error (e.g. a sibling's path-escape
   signal) is deliberately not masked by the benign fall-through
   text. Fail-closed and the nonzero exit are preserved. Anchored
   A5(d) (exact-message pin).

## Non-goals

1. No change to token literals or the CLI exit-code plane.
2. No change to G2-G5 predicates or the writer side.
3. A8 legacy hard errors (oversized declared artifact, manifest
   entry-count overflow) and the unparseable-manifest / unsafe-path
   hard errors keep no-fall-through semantics and verbatim messages.
4. No change to root enumeration or ordering (`_state_output_roots`,
   `:643-658`).

## Risk triage

- Level: compact — three small edit sites in one file (two
  raise-class changes with by-construction message identity, a
  stored attribute, one loop guard), strong existing anchor base
  (#1325 A5/A6 + #1330 composition test). Divergence from the
  issue's S/M estimate: the M half (contract re-scope) is fixture
  work, not code risk.
- Must-preserve: single-root token/message/exit identity (existing
  A5(d)/A5(e) tests verbatim, `tests/test_warm_start_chaining.py:2561`,
  `:2587`; #1330 composition test `tests/test_shud_runtime.py:6439`);
  rule-5 first-existing-root determinism (`:2787`); A8 hard errors;
  post-gate empty-list guard (`state_cli.py:179-186`); durable-reuse
  publish lane.
- Risk packs: publish-integrity (wrong-root publish is the new
  failure surface — A1/A2 assert WHICH root's artifacts publish);
  fail-closed error-path (A3/A5). Not selected: filesystem-safety
  (no new fs ops), DB/scheduler (no orchestrator surface).
- Evidence mapping: anchors A1-A7 → issue ACs (A6 protects the
  second ruling's cross-root downgrade guard; A5(d)/A7 added in
  PR-review round 1 — hard-supersedes reporting pin and
  FINAL_IC_MISSING full-string pin); floor =
  `uv run pytest -q tests/test_warm_start_chaining.py
  tests/test_state_manager.py tests/test_shud_runtime.py` +
  `uv run ruff check .` + `openspec validate
  multiroot-shadow-fallthrough --strict --no-interactive`.
