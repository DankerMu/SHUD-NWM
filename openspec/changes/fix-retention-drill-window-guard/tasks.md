# Tasks: fix-retention-drill-window-guard

Fixture level: expanded · Repair intensity: high · Issue #1207

Triage note: production change on the fail-closed gate protecting an
irreversible DROP CHUNK path — drop/delete vocabulary on the CHANGE surface
mandates expanded; high intensity because a guard bug in either direction
is costly (false PASS = unrecoverable data loss; false REFUSE = retention
outage).

Change surface (wire codes are a FOUR-surface sync per runbook §8.2
"byte-identical across code, runbook, design fixture, unit tests — same
commit"):
- scripts/node27_timeseries_retention.py (new code constant, WIRE_CODES,
  one guard block in check_drill_gate after the `drop_window is None`
  early return)
- tests/test_node27_timeseries_retention.py (new gate tests near the
  existing check_drill_gate suite; PLUS the wire-code registry tests:
  `_EXPECTED_WIRE_CODES` literal frozenset gains the new code and the
  `len(retention.WIRE_CODES) == 15` count becomes 16)
- docs/runbooks/tier-node27-timeseries-storage.md (§7.5 mitigation wording
  → machine-enforced + windows-advance rerun consequence + D5 residual
  record; §8.2 wire-code table row AND the refusal-code priority chain
  at ~:1770-1786 gains the new code at its gate position)
- openspec/changes/tier-node27-timeseries-storage/design.md (#855 fixture
  block: wire-code list gains the new code; H2 semantics paragraph
  (~:1896) updated to the machine-enforced derivation-window wording)
- scripts/node27_archive_rebuild_drill.py — NO behavior change, but the
  stale cross-reference in the DrillConfigError message (:449-461, "the
  gate ... never reads salvage_derivation.drop_window") becomes false once
  the guard lands and MUST be updated; same for the test comment at
  tests/test_node27_archive_rebuild_drill.py:3383-3386

Must preserve:
- Existing gate order STALE → FAIL → (new guard) → forcing → runs →
  db-export; all existing reasons/codes byte-identical
- Existing check_drill_gate behavior tests green; the ONLY permitted edits
  to existing tests are the wire-code registry updates listed above
- scripts/node27_archive_rebuild_drill.py zero BEHAVIOR diff (message
  string sync only; no logic change — drill side is #1177, merged)
- Both receipt schemas zero diff
- H3/H5 drop-phase semantics untouched (guard lives in the gate phase)
- The literal string "MUST contain (⊇) the retention run's drop" in §7.5 —
  pinned by tests/test_node27_archive_rebuild_drill.py:3388; the rewrite
  must keep it verbatim

Must add (per design.md D1-D5):
- `DRILL_DERIVATION_WINDOW_TOO_NARROW` constant + WIRE_CODES member
- Whole-gate containment guard with the D2 unified rule (key entirely
  absent → skip unchanged; key present but shape unusable — not a Mapping /
  missing `drop_window` / unparseable / inverted → refuse; null window →
  pass; well-formed → closed-interval containment, equality passes)
- Runbook §7.5/§8.2 sync + layer-1 residual record

Seams under test:
- `check_drill_gate(receipt, completeness_receipt, drop_window,
  max_age_days, now)` pure function (existing test seam)
- `run_retention` gate wiring (existing fake-receipt seam) for the
  refusal-surface row

Risk packs (expanded):
- Error handling / rollback / partial outputs: SELECTED — the guard is a
  new fail-closed refusal path; both false directions are the risk axis.
- Schema / columns / units / field names: SELECTED — consumes
  `salvage_derivation.drop_window` (oneOf window|null) exactly as
  archive_rebuild_drill_receipt.schema.json declares; no schema edits.
- Legacy compatibility / examples: SELECTED — no-derivation-section
  receipts (pre-#1206 AND explicit-manifest drills) must keep current
  behavior; pinned by test.
- Public API / CLI / script entry: not selected — no entrypoint change.
- File IO / path safety / overwrite: not selected — no IO change.
- Auth / permissions / secrets: not selected — no secret surface (#1213's
  redaction chokepoint untouched).
- Concurrency / locking: not selected — gate is single-pass pure logic.

## Implementation tasks

- [x] 1. Add `CODE_DRILL_DERIVATION_WINDOW_TOO_NARROW` constant and
  register it in `WIRE_CODES`.
- [x] 2. Implement the guard block in `check_drill_gate` immediately after
  `if drop_window is None: return reasons`, per design D1-D3 (containment
  check with equality passing, D2 unified rule incl. unusable-shape
  refuse), with a docstring/comment update to the gate-order line.
- [x] 3. Tests — seven rows minimum:
  (a) issue A/B replay PASS→REFUSE flip (exact windows from the issue);
  (b) no-derivation-section receipt unchanged — including the pinned
  residual that the A/B scenario still passes without the section;
  (c) `drop_window: null` passes;
  (d) containing drill window + complete evidence passes, AND the
  equality boundary explicitly: drill window EXACTLY EQUAL to the
  retention window → PASS (the §7.5 standard invocation makes equality
  the live common case; a strict-inequality bug would refuse every
  production run);
  (e) unusable-shape refusals: section not a Mapping / `drop_window` key
  missing / window unparseable / inverted — all refuse with the new code;
  (f) integration: `run_retention` surfaces the code as
  `refusal_reason` on the refused receipt;
  (g) wire-code registry: `_EXPECTED_WIRE_CODES` + count 16 updated and
  the four-surface byte-identity tests (runbook + design fixture) green.
- [x] 4. Runbook + design fixture: §7.5 replace operator-comparison
  mitigation with the machine-enforced guard description (KEEPING the
  pinned literal "MUST contain (⊇) the retention run's drop" verbatim),
  add the windows-advance operator consequence (drill receipt reuse stops
  satisfying enforcement once the retention window advances past the
  recorded drill window — rerun the drill wider), and record the D5
  residual (a)/(b)/(c) in scoped terms; §8.2 add the wire-code row AND
  insert the code into the refusal-code priority chain at its gate
  position; openspec/changes/tier-node27-timeseries-storage/design.md
  #855 block: wire-code list + H2 paragraph updated.
- [x] 5. Mutation proof on a scratch copy (never the working tree): (i)
  guard block deleted → A/B replay test fails; (ii) containment weakened to
  overlap (`drill overlaps drop` instead of contains) → A/B replay test
  fails (drill [06-18,06-19] overlaps drop [06-18,06-25]); (iii)
  no-section branch removed (treats absent section as refuse) →
  no-section-compat test fails; (iv) boundary mutation `<=`/`>=` → `<`/`>`
  → the equality-boundary row (d) fails. Capture outputs for the PR body.

## Required evidence

- Command: `uv run pytest -q tests/test_node27_timeseries_retention.py
  tests/test_node27_archive_rebuild_drill.py` all green.
- Command: `uv run ruff check .` clean.
- Mutation outputs (guard deleted / containment→overlap /
  no-section-branch removed / boundary `<=`→`<` equality mutation) in PR
  body — all four from task 5.
- Grep: `git grep -n "DRILL_DERIVATION_WINDOW_TOO_NARROW" -- scripts tests
  docs openspec` shows all four sync surfaces (constant+WIRE_CODES,
  registry tests, runbook §8.2 row+chain, design fixture #855 block).
- `git diff --stat`: retention script + retention tests + drill
  script/tests (message/comment string sync only) + runbook + design
  fixture + this openspec change; both schemas zero diff; drill script
  behavior-bearing lines zero diff.
- node-27 read-only regression (issue acceptance row 8): retention dry-run
  against the latest live drill + completeness receipts on node-27.
  Evidence MUST record which D2 branch the live receipt actually hits
  (section absent / null window / window present) and, when a window is
  present, both window literals — then assert the dry-run is not
  mis-refused. If the live receipt has no derivation section, the record
  must explicitly state the guard was NOT exercised by this regression
  (zero coverage), not merely "PASS". SSH read-only; no state change.

## Non-goals

- Layer 2 per-subject attribution; #1175 locatability; drill-side changes;
  forcing/runs union semantics; schema edits.
