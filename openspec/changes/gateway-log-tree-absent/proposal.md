# gateway-log-tree-absent (#3144)

## Why
When a job's log tree has been deleted, the Slurm gateway's `fetch_logs` cannot say so. `_resolve_log_path` falls back
to `candidates[0]` (`services/slurm_gateway/real_backend.py:1558-1561`), and `_read_log_file` maps
`FileNotFoundError` to `""` (~:1869). The response is therefore HTTP 200 with `logs=""`, which is indistinguishable
from a genuinely empty log. `SlurmLogsResponse` (`services/slurm_gateway/models.py:131-138`) has no field for it.

`persist_gateway_logs` (`services/orchestrator/chain_workspace.py:152-191`) writes whatever it receives, and records the
URI as `log_uri`. The `should_persist_logs = existing_log_uri is None` gate (~:86, ~:103, ~:125) then never fetches
again.

#3142 / PR #3143 added an automatic retention lane for root-level `W/<run_id>` trees. An orphan or late-reconciled
non-array job can now get a permanent 0-byte `log_uri`: the persisted state reports "empty log" when the truth is
"log unavailable". The legacy branch `chain_stage_execution.py:1215-1227` (no published root) hard-codes
`should_persist_logs=True` and has the same problem.

## What
1. **Gateway contract** (additive): `SlurmLogsResponse.log_tree_present: bool = True`.
   - "Bound" means `_submission_metadata_complete(record)`, not the local `run_id`, which falls back to `job_id`
     (~:536) and is never empty. The rule in `real_backend.fetch_logs`:
     - **bound record**: `False` only when no candidate log file exists, no array task logs are found, AND every job-specific log directory is
       absent. Those directories are the stored array log dir, `W/<run_id>/logs` and the manifest `run_id` lane; the
       shared `W/logs` fallback does not count. An array master with an intact tree, which never writes
       `<job_id>.out`, stays `True`.
     - **unbound record**: this covers every pre-existing job after a **gateway restart**, because `_jobs` lives only
       in memory (`real_backend.py:266`) and sacct rebuilds records without `run_id` (~:1501-1507). Here the field is
       decided by **files actually found**: `True` if `_resolve_log_path` hit an existing file or
       `_collect_array_task_logs` returned non-None (the discover lanes scan the workspace), otherwise `False`.
       Without this, the fix would be defeated for exactly the late, orphaned jobs it targets, since those almost
       always cross a restart.
   - `False` is conservative: the log is not recorded as published, and nothing wrong is persisted.
   - **Refetch bound.** A job whose tree is gone gets no `log_uri`, so every resume of its stage refetches it: the
     settled-master republish at `chain_stage_execution.py:1189-1203` and the projection branch at ~:1205. Each
     refetch costs one sacct call, plus workspace discovery scans for unbound records. This is the same retry shape
     already accepted for a failed `fetch_logs` (#1410), and it stops once the cycle is no longer a scheduler
     candidate. The implementer verifies that no pass revisits a settled, non-candidate cycle; if one does, that is
     reported, not fixed.
   - **Observed 2026-11-16:** node-22's gateway workspace root (`/scratch/frd_muziyao/nhms-prod/workspace`) holds 428
     entries, above `MAX_DISCOVERY_ENTRIES = 256` (`real_backend.py:108`). The discovery scans therefore return empty
     (`_list_bounded_names`), so after a gateway restart, unbound pre-existing jobs whose logs live only under a
     discovered lane resolve to `False`. They are not recorded, and they are refetched while still a candidate. Today
     those jobs persist `""`, which is a lie but stable. After the fix they persist nothing, which is true but means more
     refetches. Each refetch stays bounded because the over-cap scan gives up early. This is accepted. Raising the cap
     or indexing the workspace is out of scope.
   - `_resolve_log_path`'s fallback to `candidates[0]` is NOT used as the signal, because it also fires for an intact
     array master.
   - `mock_backend` and `gateway.py` carry the field through. Check `schemas/` and the frontend generated types; none
     were found for this model.
2. **Orchestrator**: only `logs.get("log_tree_present") is False` counts; a missing field or a non-bool value means
   present. In that case `persist_gateway_logs` writes nothing and **returns an explicit sentinel**: `False` only when
   it skips, while `None` keeps meaning "written", so existing test doubles that return `None` are unchanged.
   `try_publish_log_for_advertise` maps the sentinel to `advertised_uri=None` and records no `log_uri`. The wrapper
   `chain_forecast_orchestrator_runtime.py:264` has its return type updated to match.
   - This covers the sibling entries `display_log_publication_for_stage`, `display_log_publication_for_pipeline_job`,
     and the legacy branch `chain_stage_execution.py:1215-1227`.
   - A missing field (an older gateway) means present, so nothing changes.
   - A job with an existing `log_uri` is never touched.

## Rollout
- Two-node: node-22 is ff-pulled at a scheduler pass boundary, with no `uv sync`.
- The new field reaches orchestrator callers only after `nhms-slurm-gateway` restarts. Because a missing field means
  present, the order of the pull and the restart is safe.
- The gateway is restarted at a pass boundary, the same way as on 2026-11-16: confirm the new pid, a gateway
  `/api/v1/slurm/health` of 200, `nhms-compute-api` health of 200, and that the timer is restored.
- node-27 does not run the gateway.

## Non-goals
- The retention selection rules (#3142).
- Array masters always publishing an empty `<job_id>.out` log (pre-existing; file separately if it is a defect).
- Recovering deleted logs.

## Triage
- Issue type: bug, the persisted state misreports an unavailable log as empty.
- Fixture level: **standard**: an additive gateway contract field plus orchestrator gates, with a never-break-userspace
  exposure for intact array masters and empty logs.
- Risk packs: Legacy compatibility (old gateway responses, array masters, unbound records), Contract (gateway
  response).
- Blast radius: every gateway `fetch_logs` consumer and log publication. The failure modes to rule out:
  - an intact array master never gets `log_uri`, so it is refetched on every pass;
  - an empty existing log is suppressed;
  - an unbound job loses its log.

## Evidence Floor
- [x] E1 `uv run ruff check --no-cache .`
- [x] E2 Tests. Each new behaviour test is red on the old code; record it.
  - Gateway (`tests/test_real_slurm_gateway.py`, excluded from the large-file guard):
    - a bound non-array job with its `W/<run_id>/logs` deleted returns `log_tree_present False` and `logs ""`;
    - the same tree with an existing empty `.out` returns `True`;
    - an intact array master with no `<job_id>.out` but an existing log dir returns `True`;
    - an unbound record (simulating a post-restart sacct rebuild) whose log file is discoverable returns `True`;
    - the same unbound record with its tree deleted returns `False`;
    - a bound array master whose stored log directory is deleted returns `False`, and the orchestrator records no
      `log_uri`;
    - the manifest `run_id` lane's directory existing returns `True`.
  - Orchestrator (put new tests in a new file if the natural home is a non-excluded file over 1000 lines):
    - `display_log_publication_for_pipeline_job` plus `try_publish_log_for_advertise` with `log_tree_present=False` write
      no object and give `advertised_uri is None` and `log_uri` None;
    - the empty-but-present log is written and recorded;
    - the legacy branch with a missing tree writes nothing;
    - an existing `log_uri` is not overwritten;
    - a response without the field, or with a non-bool value, behaves as present;
    - `persist_gateway_logs` returns `False` only on skip and `None` when written.
  - Selector set for the diff, plus `tests/test_real_slurm_gateway.py`, `tests/test_gateway.py`, the
    chain_workspace and log-publication suites, and `tests/test_orchestration_chain.py`, all green locally.
- [x] E3 The same set passes in a node-27 temporary worktree.
- [ ] E4 After merge: node-22 is pulled at a pass boundary and the gateway restarted. `/api/v1/slurm/health` returns
  200 before and after. A live read-only `fetch_logs` of a recent terminal job whose log file exists carries
  `log_tree_present: true` with non-empty logs. **This proves only deployment and serialization**; the `False` paths
  (bound with dirs gone, unbound with nothing found) are covered by unit tests alone.
- [x] E5 `openspec validate gateway-log-tree-absent --strict --no-interactive`.
