# Design — retire-manual-retry-submit-lanes

## Route dependency graph (after)

```
retry_run
  └─ require_retry_lane_retired              (new; always raises 409 CONTROL_PLANE_MANUAL_ACTION_REQUIRED)
       └─ require_retry_control_action       (unchanged: display_readonly 409 first)
            └─ require_retry_action          (unchanged: RBAC 401/403, action pipeline.retry_run)
                 └─ _validated_run_id        (unchanged: 400 INVALID_RUN_ID)
```

FastAPI resolves sub-dependencies first, so the order 400 → 401/403 → display 409 → retired 409 holds without any
ordering code. `get_retry_service`, `get_pipeline_store` and `get_slurm_gateway` are **not** in the retry route's
graph any more. That is what turns compute_control without `DATABASE_URL` from 500 into the stable 409.

`get_pipeline_store` and `get_slurm_gateway` stay, because the cancel route uses them. `get_retry_service` also stays,
because the node-27 C2 read-only probe and four tests reference it.

The display 409 and the retired 409 share the code. They differ in `details`:
- **display:** keeps `display_mode` and the existing keys;
- **retired:** carries `run_id`, `suggested_action` and `recovery_runbook`.

The two-node E2E evidence asserts only the code and status for display, so it is unaffected.

## Test classification rule (What 4)

A test is classified by its **subject**, the behaviour its assertions pin, not by whether it calls
`attempt_manual_retry`.

| subject | action |
|---|---|
| submit-lane behaviour: pending row, Slurm submit, runtime-root resolution on submit, `submission_failed`, 503 rendering, active-guard conflict raised by the submit method, stale-pending expiry *via the route* | delete |
| route success / 202 / response shape | delete; replaced by the fail-closed pins |
| cancel consistency, cohort charge, read-blocked consumers, marker lane, auto retry, scheduler mint, job-list rendering | reseed: write the `pipeline_job` / journal rows the old call produced directly, then keep the assertions |
| RBAC / display 409 / OpenAPI security on the retry operation | keep; adjust only if the success path was part of the setup |
| one test with both a marker-lane arm and a retired-method arm | trim: drop the retired arm, keep the marker arm |
| response-model sample of the retry route | delete; the route gets a documented `FAIL_CLOSED_ROUTES` exemption |

`expire_stale_retries` and its two tests stay in PR 1. They are dead after this change, and PR 2 removes them together
with the rest of the closure.

## Spec classification rule

A requirement is affected when its MUST is reachable only from the two `attempt_manual_retry` methods or from the
route's success path. The marker lane (`record_manual_repair` → scheduler mint) also says "manual retry", so wording
alone does not decide it.

Mixed requirements become REMOVED plus ADDED under a new name that keeps exactly the surviving sentences and scenarios.
MODIFIED is used where every scenario survives with a minimal text edit.
