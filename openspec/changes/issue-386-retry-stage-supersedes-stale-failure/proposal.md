## Why

After node-22 recovered IFS `2026060800`, the original shared source-cycle
`download_source_cycle` job remained `permanently_failed`, while a linked manual
retry succeeded and the forecast cycle was restored to `raw_complete`. The next
scheduler pass still emitted failed candidate evidence from the original job,
blocking downstream automation with stale terminal evidence.

## What Changes

- Treat a successful linked manual retry for a logical cycle stage as repairing
  or superseding older failed evidence for the same stage/run.
- Preserve both the original failed job and successful retry in audit evidence,
  while ensuring only active unrepaired failures block candidate readiness.
- Add regression coverage for `download_source_cycle` failure followed by a
  successful manual retry and `raw_complete` cycle state.
- Document operator diagnosis/remediation for stale stage evidence without DB
  surgery.

## Capabilities

### New Capabilities

- `retry-stage-evidence-supersession`: readiness evidence recognizes successful
  manual retry repair jobs for shared cycle stages.

### Modified Capabilities

None.

## Impact

- Scheduler/candidate readiness evidence: `services/orchestrator/scheduler.py`,
  `services/orchestrator/chain.py`.
- Tests: scheduler/orchestration evidence and monitoring/API surfaces that
  display stage status.
- Docs: node-22/operator runbook stale stage evidence guidance.
- No frontend, display-readonly, database migration, or runtime-root retry
  submission behavior change is intended.
