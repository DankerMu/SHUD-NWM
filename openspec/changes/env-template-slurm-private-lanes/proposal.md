# Proposal — env-template-slurm-private-lanes (#3171)

Closes #3171.

## Why

`infra/env/compute.scheduler-dbfree.env.example` carries the Slurm private-lane state index
(`NHMS_SLURM_SCHEDULER_STATE_INDEX`, #3123) but not the two keys the issue names:
- `NHMS_SLURM_SCHEDULER_REGISTRY_MANIFEST`
- `NHMS_SLURM_SCHEDULER_CANONICAL_READINESS_INDEX`

The live node-22 `compute.scheduler-dbfree.env` and `compute.replay.env` set both to
`/scratch/frd_muziyao/nhms-prod/object-store/scheduler/...`. When a key is unset, the value handed to Slurm jobs falls
back to the shared key (`/ghdc/...` in the template):
- the job manifest context: `services/orchestrator/chain_manifests.py:255-264`;
- the orchestrator job env: `services/orchestrator/chain_runtime_utils.py:1059-1066`;
- the gateway job env: `services/slurm_gateway/real_backend.py:1077-1090`. The order is the gateway's own private
  key, then the manifest context, then the gateway's shared key. The gateway loads `compute.host.env`, which sets only
  the shared keys (read-only check), so the manifest context carries the dbfree env's value to the job.

The two keys are not equivalent. Read-only check on node-22, 2026-11-17:

| File | Private (`/scratch`) | Shared (`/ghdc`) |
|---|---|---|
| `registry/manifest-last.json` | 13,353,466 B, 10:35:57, sha256 `a4290c6c…` | 13,353,466 B, 10:35:59, sha256 `a4290c6c…` (identical) |
| `canonical-readiness/index-last.json` | 50,062 B, **2026-07-01 23:38** | 3,407,747 B, 2026-11-17 10:40 |

- **Registry.** The private manifest is a maintained mirror. Provider refresh writes the worker mirror
  (`scripts/scheduler_refresh/config.py:308`, `runner.py:515-517`). The manifest context requires its generation to
  match the shared manifest (`chain_manifests.py:255-259`, `require_generation_match=True`). The template must carry it,
  otherwise a rebuilt env drops the private lane.
- **Canonical readiness.** No producer writes the private index. Refresh publishes only the shared
  `config.readiness_uri` (`scripts/scheduler_refresh/runner.py:522-524`, `config.py:299`), and no generation check
  guards it (`chain_manifests.py:261-264`). The live private file is 4.5 months stale. No entry point of the
  node-22 chain reads the value: `infra/sbatch/*.sbatch` runs `workers.shud_runtime.cli`, `workers.forcing_producer.cli`,
  `packages.common.state_cli` and `nhms-canonical` (under `workers/`), and none of `workers/`, `packages/` or
  `infra/sbatch/` references a canonical readiness index. The `nhms-parse` and `publish-tiles` entries run after the
  terminal `forecast_state_save_qc` stage, outside that chain. Adding the key to the template would turn an unread, unmaintained, stale path into the documented contract.
  Leaving it unset makes a rebuilt env fall back to the shared index, which refresh keeps current.

## What

1. Add `NHMS_SLURM_SCHEDULER_REGISTRY_MANIFEST` to `infra/env/compute.scheduler-dbfree.env.example`, next to
   `NHMS_SLURM_SCHEDULER_STATE_INDEX`, with the node-22 private path. Its comment names the shared-key fallback and the
   generation check.
2. Add it to the `infra/env/README.md` `nhms-required-keys: compute.scheduler-dbfree` block, so
   `tests/test_env_templates.py` fails when the template loses it.
3. Document in the template and the README that `NHMS_SLURM_SCHEDULER_CANONICAL_READINESS_INDEX` is deliberately
   unset, and why: no producer, no reader, and the fallback is the maintained shared index.
4. Spec: ADDED requirement in `compute-scheduler-operationalization`.

Other templates, checked:
- There is no tracked template for `compute.replay.env`.
- `compute.scheduler-provider-refresh.env.example:17` already carries `NHMS_SLURM_SCHEDULER_REGISTRY_MANIFEST`. The
  refresh script's allowlist (`scripts/scheduler_file_provider_refresh_once.sh:23,81-86`) has no readiness private
  key.
- `compute.example` (the compose lane) sets the shared keys to the `/scratch` paths, so its fallback already resolves
  privately. Not changed.

## Non-goals

- No live env edits on node-22. The live `NHMS_SLURM_SCHEDULER_CANONICAL_READINESS_INDEX` pointing at the stale private
  file is reported on the issue as an ops follow-up (unset it at a pass boundary), not changed here.
- No change to the fallback code.
- The duplicate `NHMS_SLURM_SCHEDULER_STATE_INDEX` line in node-22's live `compute.replay.env` is report-only.

## Must preserve

- Every key and value already in the template and the README block.
- `tests/test_node22_scheduler_env_drift_table.py`: the table pins seven keys
  (`tests/test_node22_scheduler_env_drift_table.py:129-134`) and does not change.

## Fixture level

minimal. Risk pack: config / project setup.

## Evidence Floor

- [ ] E1 `uv run ruff check --no-cache .`; `git diff --check`.
- [ ] E2 `uv run pytest -q tests/test_env_templates.py tests/test_node22_scheduler_env_drift_table.py` passes, and the
  required-keys check fails when `NHMS_SLURM_SCHEDULER_REGISTRY_MANIFEST` is removed from the template (red shown on a
  scratch copy). The private-over-shared precedence is already pinned by `tests/test_orchestration_chain.py:1925-1936`,
  `tests/test_job_array.py:269-271`, `tests/test_slurm_array_contract.py:358-360` and
  `tests/test_db_free_auto_retry_manifest.py:85-87`; they are run unchanged. A new case in `tests/test_env_templates.py`
  parses the template with `parse_env_template` and asserts `NHMS_SLURM_SCHEDULER_CANONICAL_READINESS_INDEX` is absent;
  it is shown red when the key is added to a scratch copy. The template documents the absence only in whole-line `#`
  comments, which the parser skips.
- [ ] E3 Read-only node-22 receipt (in this proposal, Why): the template's registry value equals the live value; the
  private and shared registry manifests are identical; the private readiness index is stale; `compute.host.env` sets
  only the shared keys.
- [ ] E4 `openspec validate env-template-slurm-private-lanes --strict --no-interactive`.
