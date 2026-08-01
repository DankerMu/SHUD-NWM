# Tasks: fix-retention-drill-snapshot-binding

Fixture level: expanded · Repair intensity: high · Issue #1220

Triage note: production change on the fail-closed gate protecting an
irreversible DROP CHUNK path, PLUS a receipt schema extension and a drill
emit change — expanded is mandatory (drop/delete vocabulary on the change
surface); high intensity because both false directions are costly (false
PASS = unrecoverable data loss; false REFUSE = daily retention outage —
the D1 arithmetic is exactly about not building the second one).

Change surface (wire codes are a FOUR-surface sync per runbook §8.2):
- scripts/node27_timeseries_retention.py (new code constant + WIRE_CODES;
  helper `_drill_snapshot_binds`; one guard insertion in the db-export leg
  of check_drill_gate after the empty-derivation refusal, before the
  per-target loop; gate-order docstring update)
- scripts/node27_archive_rebuild_drill.py (derivation path records
  `db_export_windows` universe + `completeness_generated_at`; requires
  extending the frozen `SalvageDerivation` dataclass (~:262-271) at its
  single construction site (~:461-478) — `_salvage_provenance_fields`
  cannot see the completeness receipt object; explicit-manifest path
  unchanged)
- schemas/archive_rebuild_drill_receipt.schema.json (two OPTIONAL
  properties in the salvage_derivation block; `required` unchanged)
- tests/test_node27_timeseries_retention.py (gate tests near the #1207
  suite; wire-code registry: `_EXPECTED_WIRE_CODES` gains the code, count
  16→17)
- tests/test_node27_archive_rebuild_drill.py (emit-side rows: universe
  recorded unfiltered + schema-valid; existing derivation tests updated
  ONLY where the new fields change asserted receipt shapes)
- docs/runbooks/tier-node27-timeseries-storage.md (§7.5 binding rule +
  operator consequence — wording "a NEW OR CHANGED db-export window"
  (an extended existing window unbinds too) + residual update incl.
  closing fix-retention-drill-window-guard D5-(b) at window granularity;
  §8.2 table row AND priority chain between DRILL_COVERAGE_RUNS_MISSING
  and DRILL_COVERAGE_DB_EXPORT_MISSING with the empty-derivation-first
  note; §8.4 preconditions block gains the binding clause — #1207
  round-1 B1 taught this sweep; §8.7 salvage-backed-windows explainer
  (~:2063-2080) gains a one-line cross-reference to the binding rule)
- openspec/changes/tier-node27-timeseries-storage/design.md (#855 block:
  wire-code list + H2 paragraph)

Must preserve:
- Existing gate order and all existing reasons/codes byte-identical;
  empty-derivation still refuses DRILL_COVERAGE_DB_EXPORT_MISSING before
  the binding check
- #1207 guard behavior untouched (its tests stay green unmodified)
- Receipts without the section, and with the section but without
  `db_export_windows`, keep current behavior (pinned by test)
- Schema `required` lists unchanged; retention receipt schema zero diff
- The pinned §7.5 literal "MUST contain (⊇) the retention run's drop"
  verbatim (tests/test_node27_archive_rebuild_drill.py pin)
- Explicit-manifest drills still omit `salvage_derivation` entirely

Must add (per design D1-D5):
- `DRILL_COMPLETENESS_SNAPSHOT_UNBOUND` constant + WIRE_CODES member
- `_drill_snapshot_binds` with the D3 rule (absent section/field → skip;
  unusable → refuse; else exact-membership for every target)
- Emit-side universe recording with D2 normalization (unfiltered,
  deduped, sorted, string endpoints) + `completeness_generated_at`
- Schema: two optional properties
- Runbook/design sync incl. D1 statement that `completeness_generated_at`
  is diagnostics-only, never a refusal input

Seams under test:
- `check_drill_gate(receipt, completeness_receipt, drop_window,
  max_age_days, now)` pure function (existing seam)
- `run_retention` gate wiring (existing fake-receipt seam) for the
  refusal-surface row
- drill `build_receipt`/derivation path via existing drill-test fixtures
  (receipt shape + jsonschema validation)

Risk packs (expanded):
- Error handling / rollback / partial outputs: SELECTED — new fail-closed
  refusal path; both false directions are the risk axis (D1).
- Schema / columns / units / field names: SELECTED — receipt schema gains
  two optional fields; emit and gate must agree on exact names and window
  normalization; jsonschema round-trip pinned by test.
- Legacy compatibility / examples: SELECTED — three receipt populations
  (no section / section without field / section with field) each pinned.
- Public API / CLI / script entry: not selected — no entrypoint change.
- File IO / path safety / overwrite: not selected — no IO change.
- Auth / permissions / secrets: not selected — no secret surface.
- Concurrency / locking: not selected — single-pass pure logic.

## Implementation tasks

- [ ] 1. Gate side: constant + `WIRE_CODES` registration +
  `_drill_snapshot_binds` helper + guard insertion in the db-export leg
  (after empty-derivation refusal, before per-target loop) + docstring
  gate-order update.
- [ ] 2. Emit side: compute the D2 universe from the loaded completeness
  receipt (unfiltered by drop window) + `completeness_generated_at`;
  thread both into the salvage_derivation mapping. No change to the
  explicit-manifest path.
- [ ] 3. Schema: `db_export_windows` (array of `#/definitions/window`) and
  `completeness_generated_at` (string, format date-time) as OPTIONAL
  properties of `salvage_derivation`.
- [ ] 4. Tests — rows (a)-(l) minimum:
  (a) issue v1/v2 replay: recorded universe [A], gate-time completeness
  A+B, un-narrowed drill, drop [06-05,06-15] → reasons ==
  [DRILL_COMPLETENESS_SNAPSHOT_UNBOUND]; SAME receipts without
  `db_export_windows` → reasons == [] (pre-fix oracle + residual pin);
  AND the same drift with the whole `salvage_derivation` section absent →
  reasons == [] (front half of the compat contract pinned too);
  (b) identical snapshot passes; requirement shrink (recorded [A,B],
  gate-time A only) passes (kills inverted-to-superset mutants);
  (c) daily-regeneration equivalence: gate-time receipt regenerated
  (different generated_at) but same windows → passes (D1 pin:
  completeness_generated_at drift alone never refuses);
  (d) targets-scoping (fixture-review P1-2): recorded [A], gate-time
  completeness = A + new db-export/complete subject D with window
  [08-01,08-10] DISJOINT from drop [06-05,06-15] → reasons == []
  (binding judged over drop-filtered targets only, never the whole
  current universe);
  (e) one-sided changes refuse (fixture-review P2-3): target sharing
  start with a recorded window but longer end (A extended to
  [06-01,07-15]) → refuse; target sharing end but different start →
  refuse (membership exact on BOTH endpoints);
  (f) empty recorded universe refuses (fixture-review P2-4):
  `db_export_windows: []` + non-empty targets → refuse (an
  `if not recorded: return True` implementation must die);
  (g) unusable shapes refuse (parametrized: not-a-list, entry not a
  Mapping, missing/non-string endpoints);
  (h) ordering/precedence pins (fixture-review P2-5): empty-derivation
  still → DRILL_COVERAGE_DB_EXPORT_MISSING (not the new code); narrowed
  drill + snapshot drift → DRILL_DERIVATION_WINDOW_TOO_NARROW (#1207
  wins, D5-(d) tripwire); missing runs coverage + snapshot drift →
  DRILL_COVERAGE_RUNS_MISSING (sibling-style precedence pins near
  tests/test_node27_timeseries_retention.py:3872-3906);
  (i) integration: `run_retention` surfaces the code as `refusal_reason`
  on the refused receipt (schema-validated);
  (j) wire-code registry: `_EXPECTED_WIRE_CODES` + count 17 + four-surface
  byte-identity tests green;
  (k) emit side: derivation-mode drill records the unfiltered universe
  (subject outside the narrowed drill drop window still recorded) +
  `completeness_generated_at` + receipt jsonschema-valid; explicit-manifest
  drill still omits the section;
  (l) emit→gate ROUND TRIP (fixture-review P1-1): a receipt built by the
  REAL emit path (`_derive` + `_run_with_runs_cycle` fixtures,
  tests/test_node27_archive_rebuild_drill.py:2276-2345; that file already
  imports retention) judged by `check_drill_gate` against the very
  completeness receipt it was derived from → reasons contain no
  binding refusal; add a new overlapping db-export/complete subject to
  that completeness receipt → DRILL_COMPLETENESS_SNAPSHOT_UNBOUND. Kills
  any emit/gate field-name or normalization mismatch (silent permanent
  dormancy).
- [ ] 5. Runbook §7.5/§8.2/§8.4 + tier design fixture #855/H2 sync per
  change surface, incl. the D5 residual rewrite (close
  fix-retention-drill-window-guard D5-(b) at window granularity; record
  the window-granularity blind spot and the dormant-population residual).
- [ ] 6. Mutation proof on a scratch copy (never the working tree):
  (i) binding guard deleted → replay row (a) fails;
  (ii) membership weakened to "recorded set non-empty" → replay row fails;
  (iii) field-absent branch removed (absent → refuse) → compat rows fail;
  (iv) membership weakened to overlap (target overlaps any recorded
  window) → replay row fails (B [06-10,06-20] overlaps A [06-01,06-30]);
  (v) emit-side universe filtered by drill drop window → emit row (k)
  fails;
  (vi) membership judged over the whole current universe instead of the
  drop-filtered targets → targets-scoping row (d) fails;
  (vii) comparison collapsed to start-only, and separately to end-only →
  the one-sided rows (e) fail. Capture outputs for the PR body.

## Required evidence

- `uv run pytest -q tests/test_node27_timeseries_retention.py
  tests/test_node27_archive_rebuild_drill.py` all green.
- `uv run ruff check .` clean; markdownlint runbook 0 issues.
- `openspec validate fix-retention-drill-snapshot-binding --strict
  --no-interactive` and `tier-node27-timeseries-storage` both valid.
- Mutation outputs (i)-(v) in PR body.
- Grep: `git grep -n "DRILL_COMPLETENESS_SNAPSHOT_UNBOUND" -- scripts
  tests docs openspec` shows all four sync surfaces.
- `git diff --stat`: the seven change-surface files + this change dir
  only; retention receipt schema zero diff; `required` lists in the drill
  receipt schema unchanged (diff shows properties addition only).
- node-27 read-only regression (issue acceptance row 10): retention
  dry-run on node-27 with the PR branch code (scratch worktree, fetch
  only, production env, ENFORCE=0, receipt to scratch) against the live
  drill + completeness receipts. The record MUST state which D3 branch
  the live receipt hits (expected: section absent → guard skipped, zero
  live coverage of the membership logic — state this explicitly, not
  merely PASS) and assert the dry-run is not mis-refused
  (outcome != refused). SSH read-only + scratch worktree only.

## Non-goals

- Layer-2 per-subject attribution; #1175; forcing/runs semantics;
  digest/generated_at-based refusal (rejected D1); completeness-receipt
  schema changes.
