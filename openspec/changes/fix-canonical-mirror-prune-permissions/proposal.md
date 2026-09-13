# Make the canonical precipitation mirror deletable by node-27 retention (#2100)

## Why

`scripts/node27_raw_retention.py` prunes `canonical/<storage_source>/<cycle>/`
on the same watermark as `raw/`, but the mirror is produced on node-22 as
`frd_muziyao` with `0o755` directories (`publisher._chmod_tree_readable`,
`canonical_precip_copyback_backfill.DIR_MODE`) in group gid 1078, while the
pruner runs on node-27 as `nwm`, which is neither owner nor member of that
group. Since the first production tick on the canonical lane
(2026-09-13T03:35:32Z) every aged canonical cycle fails with
`PermissionError`: 22 targets, zero bytes freed, the unit sits in
`ActiveState=failed`, and #1987's window admission refuses on that state.
The mirror grows monotonically on the 1.7 TB volume shared with `pg_default`.

## What Changes

- **Producer modes.** The canonical-precip copyback lane leaves its copied
  directory trees (`<cycle>/`, `<cycle>/prcp_rate_or_amount/`,
  `grid/<grid_id>/`) at `0o775`, preserving an inherited setgid bit; files
  stay `0o644`. The `runs/` and `forcing/` copyback lanes and the backup
  clone path keep `0o755` (the forcing tree carries a default ACL where a
  wider group mode would move the mask). `ensure_traversable_copyback_directory`
  is untouched. The one-shot backfill script applies the same directory mode.
- **Existing tree (ops, not code).** A documented, idempotent sweep run on
  node-22 as the owner: `chgrp -R 1107` (shared group `nwmuser`, present on
  both hosts with `nwm` and `frd_muziyao` as members) and `chmod 2775` on
  every directory under `canonical/gfs` and `canonical/IFS`, roots first.
  `canonical/` itself is left alone so a future storage source fails closed
  (first `unlink` denied, zero bytes) until it is swept.
- **Runner and operator check.** `PermissionError` on the canonical lane is
  an incident, not the expected steady state: the rmtree handler comment and
  the "UNTIL #2100 LANDS" block in `infra/env/node27-raw-retention.example`
  are rewritten and clause 3 of the documented `jq` no longer whitelists
  `PermissionError`.
- **Runbook.** The permission model, the sweep, the new-source precondition,
  the reversal, and the post-deploy re-sweep go into
  `docs/runbooks/current-production-ops.md` §5.3; the live receipt into
  `docs/runbooks/receipts/`.

## Capabilities

### New Capabilities
- (none)

### Modified Capabilities
- `filesystem-permission-determinism`: the copyback traversal-widening
  requirement's scenarios now distinguish levels the traversal widening
  creates (`0o755`, unchanged) from `<cycle>/` and the trees the
  canonical-precip lane copies (explicit `0o2775`).
- `node27-raw-retention` (created by change
  `fix-raw-retention-lane-root-locality`, #2104, which merges first): two
  ADDED requirements — canonical mirror cycle trees are deletable by the
  retention account, and a `PermissionError` in `failed[]` is reported red.
  No existing requirement is modified: #2104's operator-check requirement
  covers only the `*_unsafe` dimension and explicitly leaves what the check
  accepts in `failed[]` to this change, so the two do not collide.

## Impact

- `services/tile_publisher/publisher.py` (`_chmod_tree_readable` gains a
  directory-mode parameter; only `_copyback_canonical_precip`'s trees use
  `0o775`).
- `scripts/canonical_precip_copyback_backfill.py` (`DIR_MODE`,
  `_ensure_target_directory`).
- `scripts/node27_raw_retention.py` (comment only),
  `infra/env/node27-raw-retention.example` (operator check + prose).
- `tests/test_tile_publisher.py`, `tests/test_canonical_precip_copyback_backfill.py`,
  `tests/test_node27_raw_retention.py`.
- `docs/runbooks/current-production-ops.md`, a new receipt under
  `docs/runbooks/receipts/`, and a supersession pointer in
  `openspec/changes/display-v2-national-timeline-precip-overlay/specs/canonical-precip-copyback/spec.md`
  where it states the backfill directory mode as `0o755`.
- Live: node-22 sweep (owner account), node-27 retention tick (the unit
  itself), post-merge node-22 `git pull --ff-only` (no `uv sync`) and one
  mirrored cycle's `stat` as the persistence receipt.
- Blast radius (disclosed, accepted): `nwmuser` on node-22 has seven human
  members who gain write on canonical cycle directories over NFS; the
  object-store root is already `775` group gid 1078 (23 members) and `raw/`
  is `777`, so this is not a posture regression. The same
  `chgrp nwmuser && chmod 2775` pattern is the runbook's existing convention
  for cross-account directories (`current-production-ops.md` hop 3).
