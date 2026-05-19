## Why

M10 created production-like validation lanes, but final production proof still needs a consolidated readiness framework that can run with current deterministic data now and consume live evidence later. CLDAS and complete real national data are out of scope for this change; readiness must be truthful about `passed`, `blocked`, `not_executed`, and `release_blocked`.

## What Changes

- Add a consolidated readiness evidence model covering auth, alert sink, rollback, Slurm, object store, source dependencies, MVT/performance, and model operations.
- Provide deterministic and opt-in live execution modes for readiness checks.
- Emit blocker artifacts and removal criteria for unavailable live dependencies.
- Add summary/report generation for release decision review.
- Keep CLDAS and incomplete real national data explicit non-blocking exclusions unless separately enabled.

## Capabilities

### New Capabilities

- `readiness-evidence-schema`
- `deterministic-readiness-lane`
- `opt-in-live-proof-lane`
- `release-blocker-summary`
- `readiness-reporting-docs`

## Impact

- Production closure validation commands, evidence schemas, docs, progress tracking, and tests.
- Consumes outputs from M17, M18, M16, and existing M10 lanes.
- Does not require CLDAS or complete national real data.

## Non-Goals

- Claiming final production readiness without live proof.
- Enabling CLDAS.
- Completing real national dataset/model coverage.
