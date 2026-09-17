## 1. Implementation
- [x] 1.1 Add per-source discharge outcome counters and advance summary schema without changing operational knobs.
- [x] 1.2 Add mixed-result source-isolation regression and cover transport failure, zero/unissued buckets, and existing CLI schema consumers.
- [x] 1.3 Update existing runbook summary contract; leave historical receipts intact.

## 2. Evidence Floor
- [x] 2.1 Fixture reviewer pass and openspec validate prewarm-source-discharge-results --strict --no-interactive.
- [ ] 2.2 On node-27, demonstrate the new mixed-source regression fails against base source and passes against changed source; uv run pytest tests/test_node27_mvt_prewarm.py -q passes.
- [ ] 2.3 Local uv run ruff check scripts/node27_mvt_prewarm.py tests/test_node27_mvt_prewarm.py passes; cross-review and CI pass at frozen SHA.

## Risk packs
- Selected Public API / CLI / script entry: v3 normal/error JSON, tasks 1.2/2.2.
- Not selected Config / project setup: no configuration changes.
- Not selected File IO / path safety / overwrite: existing output path semantics unchanged.
- Selected Schema / columns / units / field names: both counters and v3 shape, tasks 1.1/1.2/2.2.
- Not selected Auth / permissions / secrets: no auth changes; no tokens in evidence.
- Not selected Concurrency / shared state / ordering: post-executor aggregation only; scheduling unchanged.
- Not selected Resource limits / large input / discovery: no limits/discovery changes; skip exclusion preserved by 1.2/2.2.
- Selected Legacy compatibility / examples: discharge_requests and global totals preserved, tasks 1.2/1.3.
- Selected Error handling / rollback / partial outputs: transport/discovery failures and deadline skips, tasks 1.2/2.2.
- Not selected Release / packaging / dependency compatibility: no dependency change.
- Selected Documentation / migration notes: runbook v3 accounting, task 1.3.

## Ordered follow-on gate (not B acceptance)
B merge precedes a genuine node-27 cold prewarm receipt with all per-source buckets, failures, failed_count, elapsed_seconds, deadline_skipped, per-request max/p95 and effective configuration. Only then may #2121-A and #2346 step two jointly change shared knobs. Keep #2121 open after B.
