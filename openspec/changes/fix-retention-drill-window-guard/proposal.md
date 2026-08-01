# Drill derivation-window guard before irreversible DROP CHUNK (#1207)

## Why

`check_drill_gate`'s db-export leg judges each salvage-backed window against
the UNION of ALL db-export coverage tuples — tuples carry no subject
identity, and the gate never reads the drop window the drill itself recorded
(`salvage_derivation.drop_window`, machine-readable since PR #1206). With
PR #1206's `--drop-window-*` narrowing knob, a drill can legally verify only
subject A, whose full-window tuple then vouches for never-verified subject
B: the gate returns empty reasons → PASS → irreversible DROP CHUNK proceeds
on rows whose salvage manifest was never restore-verified. Read-only probe
in the issue reproduces the exact PASS→REFUSE flip. The in-PR-1206
mitigation is runbook prose asking the operator to compare two windows by
hand — the receipt already carries the fact needed for machine enforcement.

## What Changes

Layer 1 of the issue's recommended fix (layer 2 — per-subject tuple
attribution with schema change — is explicitly out of scope, separately
scheduled):

- `scripts/node27_timeseries_retention.py`:
  - New wire code `DRILL_DERIVATION_WINDOW_TOO_NARROW` (constant +
    `WIRE_CODES` registration).
  - `check_drill_gate`: after the existing `drop_window is None` early
    return, read `receipt["salvage_derivation"]["drop_window"]`; if the
    section exists and its drop window does not CONTAIN the retention drop
    window (closed-interval; equality passes), refuse fail-closed with the
    new code. Section entirely absent = no-derivation-section receipt
    (pre-#1206 OR today's explicit-manifest drills), unchanged behavior.
    `drop_window: null` = drill ran un-narrowed, guard passes. Section
    present but unusable (not a Mapping / missing `drop_window` key /
    unparseable / inverted) = refuse (nonsense evidence never counts —
    symmetric with the inverted-tuple defence in `_tuples_cover_window`).
  - Wire codes sync across FOUR surfaces in the same commit (runbook §8.2
    contract): constant+`WIRE_CODES`, registry tests
    (`_EXPECTED_WIRE_CODES` + count 15→16), runbook §8.2 table + priority
    chain, and `openspec/changes/tier-node27-timeseries-storage/design.md`
    #855 block (list + H2 paragraph).
- `tests/test_node27_timeseries_retention.py`: issue-scenario A/B replay
  (PASS→REFUSE flip), no-derivation-section compat (including the pinned
  residual: cross-subject substitution still passes without the section),
  null window pass, containment pass incl. the EQUALITY boundary (drill ==
  retention window → PASS; the §7.5 standard invocation makes equality the
  live common case), unusable-shape refusals, refusal-surface integration
  (reasons[0] → `refusal_reason`), and the wire-code registry updates.
- `docs/runbooks/tier-node27-timeseries-storage.md` +
  `openspec/changes/tier-node27-timeseries-storage/design.md`: §7.5
  replace the operator-compares-windows mitigation with the
  machine-enforced guard (keeping the test-pinned "MUST contain (⊇)"
  literal) + windows-advance rerun consequence; §8.2 code row + priority
  chain; design-fixture #855 wire-code list + H2 paragraph. Residual
  recorded in scoped terms (design D5 a/b/c: no-section receipts,
  receipt-snapshot unbinding — filed as its own issue, and
  relative-to-snapshot semantics inside a contained window); root fix
  tracked as layer 2.
- `scripts/node27_archive_rebuild_drill.py` /
  `tests/test_node27_archive_rebuild_drill.py`: string-only sync of the
  DrillConfigError message and a test comment that assert the gate "never
  reads salvage_derivation.drop_window" — false once the guard lands. No
  behavior change.

## Non-goals

- Layer 2 per-subject tuple attribution (drill receipt schema change +
  legacy-receipt degradation decision) — separate issue per #1207 meta.
- `DRILL_COVERAGE_DB_EXPORT_MISSING` locatability — #1175 (orthogonal; same
  code region, coordinate ordering: this change adds one guard block before
  the db-export leg and does not reshape existing reasons).
- Drill-side input derivation — #1177 / PR #1206, already merged.
- `forcing`/`runs` whole-window union semantics — correct by design.
- No change to `schemas/archive_rebuild_drill_receipt.schema.json` (the
  consumed field already exists) or `timeseries_retention_receipt.schema.json`
  (`refusal_reason` is a free-form min-length-1 string).
- Drill/completeness receipt-snapshot binding (residual D5-b) — filed as
  issue #1220 during fixture review; not claimed fixed here.
