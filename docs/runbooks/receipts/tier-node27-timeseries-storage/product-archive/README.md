# Product-archive live receipts (task §2.3 upstream of §6.3)

This directory holds committed live receipts from
`scripts/node27_product_archive.py` on node-27 (`nwm` user, main
worktree `/home/nwm/NWM`).

## Receipts

### `controlled-enforce-20260715T054015Z.json`

Passing node-27 receipt for issue #1065's explicitly authorized controlled
30-day product-archive enforce, run at 2026-07-15T05:40:15.662341Z UTC on
deployed head `e130949c4f9d658d9e31251e5ced135147e18712`:

| Field | Value |
|---|---|
| mode | `enforce` |
| outcome | **`success`** |
| minimum_age_days | 30 (one-invocation override) |
| production env minimum age | 45 (unchanged) |
| candidates / selected / deferred | 352 / 8 / 344 |
| bytes.source / bytes.archived | 58,423,578 / 13,708,476 |
| terminals | 8 `retired-from-existing` |
| discovery_failures | 0 |
| terminal residue | 0 |
| receipt SHA-256 | `4f72e61e8beb63476a6ca08328b84647f54f2b9606232227d8d6d4c5da64aae2` |

The preceding 30-day dry-run selected the same eight run products. A complete
selected-source audit as `nwm` covered 32 directories and 124 files and proved
parent `wx`, directory `rwx`, file readability, effective ACL masks, and sticky
ownership. Enforce then used the mover's descriptor-bound parent probes before
candidate one. Each canonical tar/manifest pair already existed from the
preserved failed attempt; the repaired idempotent path re-read and verified
each pair, reconciled only its exact matching durable guard, retired the
identical source, and left no matching guard residue. Post-run verification
proved all eight sources absent and all eight archive SHA-256 and size values
equal to their committed manifests.

This receipt closes only #1065 task 8.3. The 228 audit selectors, task 3.3
salvage, and follow-up complete inventory audit remain owned by #1070. No
compression, rebuild drill, retention dry-run/enforce, or other #856 cascade
command was run.

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
  Fixed by PR #1073: node-27 systemd proof at
  `../storage-inventory-audit/wrapper-import-live-20260713T060353Z.json`
  shows the isolated run delta reached the separately tracked #1066
  `AuditBlocked` path with zero `No module named 'scripts'` occurrences.

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

## Historical downstream impact on the retention live cascade

The retention runner (#855) hard-gates on the audit's completeness
receipt file. At the time of the first failed receipt, the consequences were:

- Retention `--dry-run` will still refuse with
  `COMPLETENESS_RECEIPT_MISSING` (same as PR #1063's committed refusal
  receipt).
- Retention enforce cannot leave the refusal path.
- Archive rebuild drill (#854) can't be exercised end-to-end because
  the archive tier stays empty even after mover runs.

The wrapper/import and canonical-prefix defects were later fixed by #1067 and
Issue #1066, and the passing controlled mover receipt above closes #1065's archive
lane. The remaining 228 DB-only gaps and complete-audit refresh are explicitly
handed to #1070; they are not silently folded into this issue or treated as
authorization to enter #856.

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

Issue #1065's product-archive mover closure is proven by the passing receipt above;
the immutable first-live failure remains the red baseline. The next archive
completeness work is #1070, not automatic entry into Step A1/B/C of #856.
The raw-retention pattern (14-day `raw/` prune) remains independent of this
cascade.
