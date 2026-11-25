# Replace a river-click pin nobody can click instead of failing the gate (#3242)

Fixture level: lite. Risk packs: none (an acceptance runbook rule, its spec
sentence and a runbook contract test; no product code). Review seats: one.

## Why

The live river-click gate takes three receipts, one each for the largest,
a median and the smallest product river network, each pinned to that
network's `…_shud_riv_000001` segment. On 2026-11-20 the pin of
`basins_byh` failed before the warmup with `HOOK_SELECTION_FAILED`,
message `hook HOOK_POINT_OCCLUDED`, `accepted_count 0`.

Diagnosis on node-27 against the live display, 2026-11-25, read-only: the
anchor is on the map canvas and no control covers it; a real click there,
and at six more points along the segment, opens a `basins_cj_main`
segment every time; 15 of the segment's 46 coordinates coincide exactly
with one of them. Two networks carry a segment on the same reach and the
click resolves to the first in layer order, so nobody can click this pin.
Which network should own such a reach is a product decision (filed as
#3390).

The gate measures how fast a click opens the curve. A pin that cannot be
clicked says nothing about that, yet today it makes the gate NO-GO, and
the rule that picks the pin has no way around it.

## What changes

- `frontend-river-click-live-evidence`, requirement "Live and mocked
  browser evidence MUST remain distinct": the three-receipt sentence gains
  the replacement rule and three scenarios.
- `docs/runbooks/node-27-bringup-checklist.md`: the signature of an
  unclickable pin, the replacement rule, and a pin-discovery script that
  also writes every product network by size.
  `docs/runbooks/tier-node27-timeseries-storage.md` points at it.
- `apps/frontend/src/__tests__/runbookContract.test.ts` pins the rule in
  the runbook.

## Decisions

- **D1 The signature is what the receipt can show**: failure code
  `HOOK_SELECTION_FAILED`, message exactly `hook HOOK_POINT_OCCLUDED`,
  `warmup_count` 0 and `accepted_count` 0. It covers both checks of the
  hook (a control over the anchor; the product click resolving to another
  feature), which a receipt cannot tell apart. Replacing the pin is right
  for both: in either case a click at the pin's anchor does not reach the
  segment.
- **D2 Nothing else may replace a pin.** Once the warmup click was
  accepted the pin is clickable, and a later failure is the gate's
  finding. This keeps "never rerun until green".
- **D3 The gate still needs three PASS receipts**, one per role. A role
  may replace its pin twice; after that, or with no unused network left,
  the gate is NO-GO.
- **D4 The hook and the lane are not changed.** Telling the two checks
  apart in the receipt would need a new closed code and a schema version;
  it is reported as a finding.

## Must preserve

- Every other FAIL or BLOCKED stays NO-GO.
- `pins.tsv` and the discovery commands' behaviour; the receipt schema;
  the hook's checks.

## Not in this change

- #3390: which network owns a shared reach.
- The hook fits the map with 48 px padding, less than the bottom control
  bar's height: 12 of this segment's 46 coordinates fall under the bar.
  The anchor is the middle coordinate, so it is rarely affected. Reported.
