# Design: fix-retention-drill-window-guard (#1207)

## D1 — Guard placement and scope: whole-gate, immediately after the
## `drop_window is None` early return

The issue's recommended layer 1 is a single-point containment check:
"若存在且不包含 retention 的 drop window，直接拒绝". We follow it literally —
the guard runs BEFORE the forcing/runs/db-export coverage legs, not inside
the db-export leg only.

Rationale: the drill's recorded `salvage_derivation.drop_window` is the span
the drill DECLARED it judged. Evidence from a run that declared a narrower
judgment span than what retention wants to drop cannot vouch for this drop,
period. Scoping the guard to the db-export leg alone would encode the
present-day mechanism ("narrowing only affects salvage derivation") into the
gate; if a future drill change narrows other legs too, a leg-scoped guard
silently rots. Whole-gate placement is the conservative reading.

Accepted cost (recorded, fail-closed direction; fixture-review P2-4 made
this honest): the guard is a pure window-proxy. It refuses whenever the
recorded drill window does not contain the retention window, EVEN when the
evidence would in fact have been sufficient — e.g. drill window
[06-20, 06-21] overlaps and derives BOTH subjects A and B
(closed-interval overlap, drill :427-430), both restore-verified, yet
retention window [06-18, 06-25] is not contained → refuse. Also refuses a
narrowed drill whose completeness receipt has no db-export subject in the
retention window (forcing/runs evidence alone would have passed today).
Operator consequence that MUST land in §7.5: a drill receipt reused within
`drill_max_age_days` stops satisfying enforcement the moment the retention
drop window advances past the recorded drill window — rerun the drill with
a window ⊇ the new retention window. Refusing is the conservative
direction before an irreversible DROP; the remedy is always "rerun the
drill wider".

## D2 — Unified rule for `salvage_derivation` (fixture-review P2-3)

One rule, two halves, both directions explicit:

- **Key `salvage_derivation` entirely ABSENT** → guard skipped, behavior
  unchanged. Neutral name: "no-derivation-section receipt". Two receipt
  populations legitimately lack the section: receipts predating PR #1206
  AND today's explicit-manifest drills (`--salvage-manifest` without
  `--completeness-receipt` → `_salvage_provenance_fields` returns None,
  scripts/node27_archive_rebuild_drill.py:2091-2098). Do NOT call this
  "legacy" anywhere operator-facing.
- **Key PRESENT but shape unusable** → refuse
  `DRILL_DERIVATION_WINDOW_TOO_NARROW`. Unusable = section not a Mapping,
  `drop_window` key missing (schema-required, so this is a
  schema-invalid shape), window not a Mapping/`null`, unparseable
  `start`/`end`, or inverted (`end < start`). A section that exists but
  cannot be judged never counts as evidence.
- **`drop_window` is `null`** → guard passes (drill ran WITHOUT narrowing;
  schema `oneOf: [window, null]`).
- **Window present and well-formed** → closed-interval containment
  required: `drill.start <= drop.start AND drill.end >= drop.end`.
  Equality on either side PASSES — the runbook §7.5 standard invocation
  passes the §7.3 step 3 interval verbatim, and that interval is a
  documented conservative SUPERSET of the runner's own drop window (the
  runner also intersects eligible chunks with the completeness
  `coverage_bounds`), so a live drill records a window EQUAL to or WIDER
  than the retention one, never narrower. Equality is the tight end of
  that range; a strict inequality would refuse it.

## D3 — Reachability note and code choice

`load_drill_receipt` (scripts/node27_timeseries_retention.py:493-521)
jsonschema-validates the whole receipt with a FormatChecker, so on the
production path missing keys / wrong types / bad ISO all surface as
`DRILL_RECEIPT_MISSING` before the gate runs. The refuse-on-unusable
branch in D2 is defence-in-depth at the pure-function seam; the one shape
schema cannot express and therefore IS reachable in production is the
inverted window (`end < start`). Tests cover both anyway. We deliberately
do NOT reuse `DRILL_RECEIPT_STALE` (means age/parse of `generated_at`) or
add a second new code (wire-code inflation; #1175 owns locatability).

## D4 — Wire code and surfaces

`DRILL_DERIVATION_WINDOW_TOO_NARROW` joins `WIRE_CODES` (the registry that
pins the wire vocabulary) and the runbook §8.2 code table.
`refusal_reason` in the retention receipt schema is a free-form
min-length-1 string, so no receipt schema change. The gate caller already
publishes `drill_reasons[0]` as the refused receipt's `refusal_reason` —
the new code surfaces on both receipt and stderr summary through existing
plumbing (integration row pins this).

## D5 — Residual after layer 1 (recorded, not fixed here; scoped per
## fixture-review P1-2)

Layer 1 blocks exactly one false-PASS mechanism: the operator narrowing
knob. Cross-subject tuple substitution SURVIVES on at least three paths,
all recorded in §7.5:

- (a) **No-derivation-section receipts** (pre-#1206 receipts AND today's
  explicit-manifest drills, which never write the section) — the guard is
  skipped entirely; the hand-narrowed-manifest path §7.5 already forbids
  in prose remains prose-only.
- (b) **Receipt-snapshot unbinding**: the gate never compares the drill's
  recorded `completeness_receipt_path`/snapshot against the completeness
  receipt IT loads. An un-narrowed drill (null window → guard passes)
  derived from an OLD completeness snapshot cannot see a subject B added
  by a later regeneration; A's wide tuple still substitutes. Pre-existing
  gate defect, filed as issue #1220 during fixture review — NOT claimed
  fixed here.
- (c) **Inside a contained window**, substitution is only harmless
  RELATIVE TO the completeness snapshot the drill consumed (all subjects
  overlapping the drill window were derived and restore-verified from
  THAT snapshot); it is not an absolute guarantee — see (b).

Layer 2 (per-subject attribution) is the root fix for all three,
separately scheduled per the issue. §7.5's residual paragraph must state
(a)-(c) in these scoped terms — no "the guard closes the gap" phrasing.

## Alternatives rejected

- Leg-scoped guard (db-export only): see D1 — encodes today's mechanism,
  rots silently.
- Layer 2 only (skip layer 1): requires drill receipt schema change and a
  legacy-degradation decision; live retention outage until a new drill runs
  (issue records this trade-off; rejected there too).
- Clipping targets to the drill window instead of refusing: would convert a
  coverage question into silent scope reduction of an irreversible DROP —
  the opposite of fail-closed.
