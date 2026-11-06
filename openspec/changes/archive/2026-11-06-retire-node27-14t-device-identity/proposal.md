# Retire the node-27 14T device identity from governance and PGDATA relocation

## Why

On 2026-11-05 node-27's 14T RAID `/dev/md0` (formerly mounted at `/data/GHDC`)
was retired. PGDATA was rebuilt by fresh initdb at `/home/nwm/nhms-primary/pgdata`
on the `/home` LV, and `/data/GHDC` is now a plain directory on the root LV.
PR #3055 corrected docs and templates only (ADR 0002 Revision 2026-11-05). Code
and the `node27-pgdata-relocation` spec still treat that volume as live:

- `packages/common/node27_resource_governance_collection.py` sampled
  `/data/GHDC` as the `filesystem.filesystems.cold` entry, which now only
  re-measures `/` and double-counts `root`.
- `packages/common/node27_pgdata_host.py` required the target filesystem to be
  `/dev/md0` mounted at `/data/GHDC`, and `packages/common/node27_pgdata_migrate.py`
  froze the #2240 production identity (source `/home/nwm/nhms-pgdata`, target
  under `/data/GHDC`). The ADR says the tool must not run until re-contracted,
  but nothing enforced that.
- Spec scenarios describe `/data/GHDC` as the current large volume.

Issue: #3059 (with the #3060 runbook recipe alignment in the same PR).

## What Changes

- Governance stops sampling `/data/GHDC`; the `cold` key is removed from
  `filesystem.filesystems`. The only reader was a test; `root`, `home`,
  `repo_root_fs`, `object_store_fs` and `pgdata_root_fs` are unchanged.
- The #2240 relocation tool is retired for production (option "retire", not
  "parameterize the device identity"): without `--disposable-root`, `plan`
  reports `production PGDATA relocation is retired ...` as its only blocker
  without any container/unit/database command, and every mutating action exits non-zero with
  that reason before the lifecycle lock. Unlisted state/action pairs keep
  refusing with the closed-table reason. `Host.health` no longer carries the
  `/dev/md0` RAID/SMART production branch. Disposable mechanics are unchanged.
- The `Placement-aware observation and truthful evidence` scenarios stop naming
  `/data/GHDC` as current storage; a new requirement records the retired
  production identity.

## Out of Scope

- node-27 deployment env (already changed by #3054) and any physical migration.
- Re-contracting the relocation tool for a new source/target pair.
- Deleting `packages/common/node27_pgdata_evidence.py` (it loses its production
  caller; reported as a follow-up).
- `tests/test_node27_resource_governance.py` and `tests/test_node27_pgdata_migrate.py`
  are over the 1000-line large-file guard and not excluded, so their
  `/data/GHDC` stub strings (monkeypatched dictionaries and replacement tokens,
  not observations) are left untouched.
