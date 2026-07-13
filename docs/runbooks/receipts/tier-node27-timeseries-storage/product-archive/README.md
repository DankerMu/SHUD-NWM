# Product-archive live receipts (task §2.3 upstream of §6.3)

This directory holds committed live receipts from
`scripts/node27_product_archive.py` on node-27 (`nwm` user, main
worktree `/home/nwm/NWM`).

## Receipts

### `first-live-run-20260713T043808Z.json`

First-ever live invocation of the node-27 product-archive mover, run at
2026-07-13T04:38:08.475013Z UTC as part of #856 Step A0 (retention live
cascade bootstrap). Provenance:

| Field | Value |
|---|---|
| generated_at | `2026-07-13T04:38:08.475013Z` |
| mode | `dry-run` (default; no flag override) |
| outcome | **`failed`** |
| cutoff | `2026-05-29T04:38:08.475013Z` (45-day archive age gate) |
| minimum_age_days | 45 |
| per_tick_bound | 8 |
| bytes.archived | 0 |
| bytes.source | 0 |
| candidates | 0 |
| deferred | 0 |
| discovery_failures | **1448** |
| free_space | `band=clean, free=706 GiB, warn=300 GiB, refuse=150 GiB` |
| schema_version | 1.0 |

Node-27 setup done for this run (recorded here so it's reproducible):

- `/home/ghdc/nwm/archive` created by operator via `sudo mkdir + chown
  nwm:nwm` (dir was 1103/nfsdata before; `nwm` couldn't write to it).
- `/home/nwm/node27-product-archive-logs` +
  `/home/nwm/node27-storage-inventory-audit-logs` created by `nwm`.
- `/home/nwm/NWM/infra/env/node27-product-archive.env` (mode 0600) +
  `/home/nwm/NWM/infra/env/node27-storage-inventory-audit.env` (mode
  0600) copied from examples with real values.
- `nhms-node27-product-archive.{service,timer}` and
  `nhms-node27-storage-inventory-audit.{service,timer}` copied to
  `~/.config/systemd/user/` (byte-identical to repo
  `infra/systemd/*.service`) + `daemon-reload` + `enable` (without
  `--now`).
- `/home/nwm/NWM` main worktree was on private branch
  `feat/issue-999-node27-rehearsal-receipt` @ `814dff9f` with 4
  unpushed commits + 5 uncommitted evidence files; hard-synced to
  master `ad187e55` after preserving the local tip at branch
  `preserve/issue-999-node27-rehearsal-receipt-20260713` and stashing
  the uncommitted work at `stash@{0}` on node-27.
- `uv sync --all-extras --dev` run in `/home/nwm/NWM` to install
  `jsonschema` + other missing runtime deps.
- After capturing evidence, both timers were `stop`-ed + `disable`-d
  to avoid unattended re-fires while the underlying bugs are fixed.

## What this receipt proves

- ✅ **End-to-end wiring works**: systemd user unit → wrapper →
  env-file source → Python entrypoint → receipt emission +
  jsonschema-conformant output. Every wrapper preflight guard passed
  (env-file mode 0600, absolute paths, zstd executable check, etc.).
- ✅ **Free-space guard operates**: `free_space.band=clean` computed
  against real disk (706 GiB free vs 300 GiB warn / 150 GiB refuse).
- ✅ **Archive tier ownership resolved**: `/home/ghdc/nwm/archive/` is
  now writable by `nwm` (was structurally blocked before this run;
  documented as an operator-driven `sudo mkdir + chown` step).

## What this receipt reveals (3 latent bugs; retention live cascade blocked)

Under the same commit that adds this receipt, three follow-up bug
issues were filed via issue-scribe:

- **Bug #1 — mover discovery cannot handle real object-store shape**:
  1448 forcing/runs/states candidates fail with 6 distinct reasons:
  `forcing manifest file URI escapes its exact package leaf`
  (hundreds), `run manifest identity/outputs do not bind run
  directory` (middle-hundreds), and 4 `Permission denied:
  'basins_*_shud'` combos under `states/{gfs,IFS}/` (nwm not in
  group nfsdata). Unit tests injected fake object-store walks and
  never exercised the real forcing/`<src>/<cycle>/basins_<basin>_vbasins/basins_<basin>_shud/`
  layout — same `Class A fake-oracle-in-tests` pattern as #854 R1 A1
  / #855 R1 C1. Tracked as
  [#1065](https://github.com/DankerMu/SHUD-NWM/issues/1065).

- **Bug #2 — audit systemd deployment path can't import `scripts.`**:
  `ModuleNotFoundError: No module named 'scripts'` under `systemctl
  --user start nhms-node27-storage-inventory-audit.service` because
  `scripts/` has no `__init__.py` and the wrapper
  `node27_storage_inventory_audit_once.sh` doesn't export
  `PYTHONPATH`. Workaround verified: `PYTHONPATH=/home/nwm/NWM
  /home/nwm/NWM/.venv/bin/python ...` progresses past the import (into
  Bug #3). systemd-wrapper-invariant drift; fix at the wrapper contract
  so this class cannot recur across sibling wrappers. Tracked as
  [#1067](https://github.com/DankerMu/SHUD-NWM/issues/1067).

- **Bug #3 — audit URI prefix mismatch + missing receipt emission on
  `blocked`**: with Bug #2 worked around, audit exits with stdout JSON
  `{"error_type":"AuditBlocked","message":"object URI outside
  configured prefix: s3://nhms/...","status":"blocked"}` — env
  configured `s3://nhms-object-store` but DB stores `s3://nhms/`. Also:
  `blocked` outcome does NOT write the configured
  `NODE27_STORAGE_INVENTORY_RECEIPT_PATH`; only writes to stdout,
  breaking the "always emit receipt" invariant retention (#855) relies
  on. Tracked as
  [#1066](https://github.com/DankerMu/SHUD-NWM/issues/1066).

## Downstream impact on the retention live cascade

The retention runner (#855) hard-gates on the audit's completeness
receipt file. As long as the audit cannot produce a receipt against the
real object-store layout:

- Retention `--dry-run` will still refuse with
  `COMPLETENESS_RECEIPT_MISSING` (same as PR #1063's committed refusal
  receipt).
- Retention enforce cannot leave the refusal path.
- Archive rebuild drill (#854) can't be exercised end-to-end because
  the archive tier stays empty even after mover runs.

**Step A0 evidence goal: partially achieved** — end-to-end wiring
demonstrated + first live receipt schema-conformant + free-space guard
operational. **Step A1/B/C: structurally blocked** on Bug #1/#2/#3
landing.

## Reproduction

On node-27 as `nwm`:

```bash
# Preconditions: /home/ghdc/nwm/archive exists (nwm-writable);
# infra/env/node27-product-archive.env populated + mode 0600;
# uv sync --all-extras --dev already run in /home/nwm/NWM.
cd /home/nwm/NWM
set -a && . /home/nwm/NWM/infra/env/node27-product-archive.env && set +a
/home/nwm/NWM/.venv/bin/python /home/nwm/NWM/scripts/node27_product_archive.py
# exit code non-zero; outcome=failed; discovery_failures=1448 under 6 reasons
```

Or (equivalently, via systemd):

```bash
systemctl --user start nhms-node27-product-archive.service
# unit fails; systemd.err empty (wrapper preflight passed);
# receipt at /home/nwm/node27-product-archive-logs/receipt.json is the
# authoritative artifact
```

## Status

Step A0 has landed as much as it can under the current code state.
Continuing to Step A1 (compression migration + timer) is not
recommended until Bug #1/#2/#3 are all fixed — the archive-completeness
gate is a prerequisite for every downstream retention step. Coordinated
with the operator: the raw-retention pattern (14-day `raw/` prune)
already runs correctly on node-27 and does not depend on this cascade.
