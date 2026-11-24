# State the retry job id format the code uses

## Why

`job-retry-mechanism`, requirement "Retry Identity — Same run_id, New
slurm_job_id", scenario "Retry gets new job_id", says a retry gets a new
`job_id` (UUID). Production has never minted a UUID there: every minting path
builds the deterministic string `<base>_retry_<n>` through
`services/orchestrator/retry_identity.py` (`retry_job_id`,
`RETRY_JOB_ID_MARKER`), and later requirements of the same spec depend on the
suffix form (#3284, found in the #3227 Stage 3 review).

## What Changes

- The scenario states the `<base>_retry_<n>` form, that `n` is at least 1,
  and that the last suffix is the authoritative attempt when suffixes stack.
- No code change, no change to the id format or the minting logic.

## Impact

- Spec: `job-retry-mechanism` (one scenario of one requirement).
