# Design notes: retire-cancel-surface

## Order of work

```text
commit A  tests only     vehicle tests moved, route untouched, everything green
commit B  the deletion   backend + OpenAPI + generated types + frontend + their tests + status vocabulary
commit C  docs/specs     docs, archive (orchestrator)
```

Commit A must not delete a cancel test: old and new stand side by side until commit B.

## Blast radius

- **Wire**: one path and its schemas leave OpenAPI. No in-repo caller besides the button (`scripts/`, `services/`,
  `workers/` do not call it). An old browser bundle that posts to the path receives 405 instead of 409.
- **RBAC**: `pipeline.cancel_run` leaves the canonical action ids. Policies keyed on it are in
  `packages/common/auth_policy.py` only.
- **Readonly validation**: the manual-action probe list loses its cancel probe. Any receipt schema or validator under
  `schemas/` or `scripts/` that enumerates the probes follows (task 6.2 sweeps `schemas/`).
- **Count pins**: two tests pin route counts (33 → 32 and 37 → 36 in the response-model preservation and schema-parity
  tests). No test pins operation, path or schema counts.
- **Status vocabulary**: two sets and their parity rows (D4). `tests/test_status_literal_scan` refuses stale allowlist
  entries, so the allowlist follows.

## Symbols that must stay although they sit next to the deleted code

`_table_columns`, `get_slurm_gateway`, `SlurmGatewayError`, `_safe_redacted_payload`, `_public_error_message`,
`_job_payload`, `_ACTIVE_JOB_STATUSES`, the retry stub and `require_action` for `pipeline.retry_run`.

## Rollback

`git revert` of commit B restores the route, the button and the vocabulary; commit A's moved tests stay valid either
way.
