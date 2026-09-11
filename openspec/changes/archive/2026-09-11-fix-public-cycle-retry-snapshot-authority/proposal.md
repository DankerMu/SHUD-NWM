# Fix public-cycle retry snapshot authority (#1356)

## Why

Two concurrent public forecast-cycle passes can select the same absence-permitted master but derive retry IDs from different later queries. Distinct retry keys then both win the existing exact-ID reservation, causing two gateway submissions. Node27 reproduced the original failure on untouched base b7cdce635a62dd30badbbf17a8ed625157224ad4; scheduling-only instrumentation confirmed the mixed-snapshot mechanism. User explicitly authorized this independent repair before resuming #2208.

## What changes

Pass the same jobs snapshot used for stage selection into retry-ID derivation at all three callsites. Preserve explicit retry-attempt precedence and existing stage/prefix/suffix logic; remove the second query, not the reservation protection. Add deterministic public-seam interleaving coverage and preserve natural concurrent regression/real journal behavior. Migrate two direct test callers. No journal schema, locks, CAS, Slurm payload, retry eligibility or production deployment change.

## Impact

Production: chain_forecast_execution.py and chain_forecast_orchestrator_cycle.py. Tests: orchestration chain and two direct file-journal callers; existing selector rules unless an actual missing edge is demonstrated. Expanded fixture with explicit submit-once invariant and independently reviewed risk coverage. #2208 remains on its separate pushed branch and blocked until this fix and its original matrix pass.
