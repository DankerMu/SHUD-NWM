## Why
#2121-B must attribute failed discharge prewarm requests before a genuine cold receipt can inform #2121-A and #2346 step two. Global failures are truncated to twenty entries and cannot provide complete per-source attribution.

## What Changes
- Add discharge_ok and discharge_failed to every per_source entry; retain discharge_requests.
- Advance emitted summary identity from nhms.node27-mvt-prewarm.v2 to v3, including CLI failure envelopes.
- Preserve request scheduling, limits, global outcomes and exit semantics.

## Capabilities
### New Capabilities
- `prewarm-source-outcomes`: complete per-source discharge outcome accounting.
### Modified Capabilities
None.

## Impact
scripts/node27_mvt_prewarm.py, tests/test_node27_mvt_prewarm.py and its runbook contract. No production knobs change. This slice does not close #2121.

Issue type: bugfix
Fixture level: expanded
Upstream suggested level: absent (expanded: script output schema)
Blast radius: prewarm JSON consumers and operational attribution
Selected risk packs: Public API / CLI / script entry; Schema / columns / units / field names; Legacy compatibility / examples; Error handling / rollback / partial outputs; Documentation / migration notes
Evidence floor: mixed outcomes and source isolation, skipped/unavailable zero buckets, node-27 targeted pytest red/green; local ruff and strict OpenSpec validation.
