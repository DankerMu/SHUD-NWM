# Record the compute role on w3 and w2

## Why

Owner decision 2026-11-23 (#3345, after #3343): NHMS compute left the shared
host `210.77.77.22` and runs on two dedicated LAN hosts behind node-27, `w3`
(Slurm controller, compute, scheduler timers, Slurm Gateway, scheduler journal)
and `w2` (compute, `workspace/runs`). The migration reproduced node-22's
uid/gid, absolute paths, partition and account, so no code, env key or unit
name changed. The repository still tells agents and operators to ssh to
`210.77.77.22` for compute work, and nothing in it describes the storage
layout, the loopback-NFS rule, or the units that exist only on the two hosts.

## What Changes

- `production-topology-contract`: the governed-topology requirement names the
  hosts of the compute role, keeps `node-22` / `node22` as the role's codename
  in identifiers, and routes Slurm scheduling verification to `w3`.
- `production-ops-readiness`: the current production ops runbook carries a
  banner that maps "node-22" to the compute role on `w3` / `w2`.
- `python-environment-truth`: the active-environment requirement describes the
  shared `.venv` of the compute role and the one synchronization window.
- Agent instructions (`instructions/agents/shared.md`, regenerated `CLAUDE.md`
  and `AGENTS.md`): host table, oracle routing, development loop and the Python
  rule for the compute role's shared `.venv`.
- New runbook `docs/runbooks/w2-w3-compute-topology.md`; three receipts under
  `docs/runbooks/receipts/` (two lock-interop matrices, SHUD output
  comparison).
- `nhms-w3-journal-backup.service` / `.timer` checked in byte-identical to the
  deployed copies and registered in the unit manifest.
- Banners on the role-boundary doc and on the runbooks that still narrate the
  compute role as "node-22".

No runtime behavior changes.

## Decisions

- **Role codename stays.** Renaming `node22_*` scripts, `nhms-node22-*` units,
  env keys and `nhms.node22.*` receipt schema ids would touch deployed units,
  the journal and historical receipts for no operational gain. The contract
  states once that the codename denotes the compute role. This follows the
  proposal in the issue body; the owner's blanket authorization for this
  program covers it, and it has not been confirmed item by item.
- **Banner now, narration sweep later.** This change adds a banner to each
  runbook that still narrates "node-22"; it does not rewrite their bodies.
  The repository-wide narration sweep (production-ops pages, other specs, code
  comments, the guards that pin that prose) is a separate, multi-PR piece of
  work that goes through `stage-change-pipeline`. Two things are fixed now
  because a banner cannot carry them: the Python execution notes at the top of
  `current-production-ops.md` and `node22-control-plane-manual-recovery.md`
  (they would state the 3.12.7 environment about `w3`), and the literal
  `ssh ... 210.77.77.22` commands in the pages, which the banner redirects to
  `w3`.
- **Python rule for the compute role.** `w3`'s `.venv` is Python 3.11.15, the
  repository pin, so the node-22 3.12.7 exception (#1831) no longer describes
  the active checkout. The prohibition itself stays, for a different reason:
  the `.venv` under `/scratch/frd_muziyao/NWM` is reached over NFS by Slurm
  jobs on both hosts and by the scheduler timers. Routine work never runs
  `uv sync` or bare `uv run` there; observation uses `uv run --no-sync` or the
  exact interpreter. Syncing the environment is allowed only inside a deploy
  that changes `uv.lock`, with the scheduler timer stopped, no pass in flight
  and no NWM job in the Slurm queue. The retired host keeps a one-line "do not
  run uv here" rule.
- **Journal-backup units are registered in `infra/systemd/node22/MANIFEST.json`.**
  `scripts/node22_unit_drift_check.py` reports any deployed unit that the
  manifest does not list as untracked, and the two units live in the same
  user systemd directory as the listed ones.

## Not in this change

- Renaming anything that carries the `node22` codename.
- Rewriting the bodies of `docs/runbooks/production-ops/*` and other runbooks;
  rewording the other specs that say "node-22".
- Teaching the topology guard predicates about `w3` / `w2` (they fire on
  `node-22` only). Tracked as a follow-up issue.
- Checking in the bundle-based deploy helper used on `w3`, and the system-scope
  `nhms-mounts-ensure` units present on both hosts; the runbook describes the
  deploy steps and reproduces the two unit files.
- #3344, #3236, yd-viewer docs, storage purchases.
- The umbrella bookkeeping line in
  `openspec/changes/redesign-orchestration-core/tasks.md` (issue scope item 6)
  rides in that change's final push (PR #3352), because that push archives the
  file.

## Impact

- Specs: `production-topology-contract`, `production-ops-readiness`,
  `python-environment-truth`.
- Docs: `instructions/agents/shared.md`, `CLAUDE.md`, `AGENTS.md`,
  `docs/governance/ROLE_BOUNDARY.md`, `docs/runbooks/current-production-ops.md`,
  `docs/runbooks/two-node-deployment-overview.md`,
  `docs/runbooks/node22-control-plane-manual-recovery.md`,
  `docs/runbooks/production-ops/service-bringup.md`, the new runbook and
  receipts.
- Infra: `infra/systemd/nhms-w3-journal-backup.{service,timer}`,
  `infra/systemd/node22/MANIFEST.json`.
- Tests: `tests/test_node22_entrypoint_invariant.py` (instruction contract
  oracle), `tests/test_node22_unit_tracking.py` (manifest entries).
