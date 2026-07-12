# Runbook: Tier Node-27 Timeseries Storage

Operation, rollback, and cadence rationale for the node-27 archive lane
(product mover + storage inventory audit) delivered under
`openspec/changes/tier-node27-timeseries-storage`.

- Design record: `openspec/changes/tier-node27-timeseries-storage/design.md`
- Architecture record: `docs/adr/0002-node27-timeseries-hot-cold-tiering.md`
- Display carve-out: `docs/adr/0001-display-timeseries-carveout.md` (the
  archive resolver is never imported by `apps/api/**` or `apps/frontend/**`).

The mover and audit share the 1.7 TB volume that also backs
`/home/ghdc/nwm/object-store` and `/home/ghdc/nwm/archive`. Free-space
watermarks defend that shared volume — the mover refuses enforce before
touching any source when free bytes fall below the configured refuse
threshold.

## Install (node-27, `nwm` user)

All operations run as the `nwm` user under systemd `--user`. Do NOT install
system-level (root) units for this lane.

1. Create the runbook receipt directories:

   ```
   mkdir -p ~/node27-product-archive-logs ~/node27-storage-inventory-audit-logs
   ```

2. Copy the env examples into place and lock them down:

   ```
   cp /home/nwm/NWM/infra/env/node27-product-archive.example \
      /home/nwm/NWM/infra/env/node27-product-archive.env
   cp /home/nwm/NWM/infra/env/node27-storage-inventory-audit.example \
      /home/nwm/NWM/infra/env/node27-storage-inventory-audit.env
   chmod 0600 /home/nwm/NWM/infra/env/node27-product-archive.env \
              /home/nwm/NWM/infra/env/node27-storage-inventory-audit.env
   ```

   Fill in `DATABASE_URL` for the audit env with a read-only role
   (`nhms_display_ro` or equivalent). A superuser or write-capable role in
   this env is a documented rollback / lint finding — the audit does not
   need write access and the receipt runbook must reject the file if the
   role is not read-only.

   The governance env now shares three archive vars with the mover and
   audit envs — `NHMS_ARCHIVE_ROOT`, `NHMS_ARCHIVE_FREE_SPACE_WARN_BYTES`,
   and `NHMS_ARCHIVE_FREE_SPACE_REFUSE_BYTES`. All three env files MUST
   declare identical values; the wrappers source only their own env file,
   so drift means governance reports a different band than the mover
   actually enforces. See "Free-space watermark tuning" below for the
   band semantics.

3. Install the four user units and enable the timers. Order matters only
   because the audit timer must see the mover's latest final leaves:

   ```
   systemctl --user daemon-reload
   systemctl --user enable --now nhms-node27-product-archive.timer
   systemctl --user enable --now nhms-node27-storage-inventory-audit.timer
   ```

4. Copy or update `infra/env/node27-resource-governance.env` from the
   extended `.example` so it declares `NHMS_ARCHIVE_ROOT` and the two
   `NHMS_ARCHIVE_FREE_SPACE_*_BYTES` vars — matching the mover and audit
   env files verbatim. Without this the governance receipt will report
   `archive_root.status = "skipped"` and the archive band will not
   appear. Then register the four new units with
   `nhms-node27-resource-governance` via the shared `DEFAULT_SERVICES`
   list — no code action required beyond deploying the updated
   `scripts/node27_resource_governance.py`. The next governance audit
   tick will report their service/timer state, and — once the governance
   env carries the shared archive vars — the archive root free-space
   band as well. See "Free-space watermark tuning" for band semantics
   and "Refuse-threshold behavior" for what a `refuse` band triggers.

## Timer cadence order (UTC)

The three related timers are staggered so each receipt is fresh when the
next tick runs:

| Order | Timer                                        | OnCalendar         | Rationale |
|-------|----------------------------------------------|--------------------|-----------|
| 1     | `nhms-node27-product-archive.timer`          | `03:20:00 UTC` daily | Mover finalizes leaves before audit scans them. |
| 2     | `nhms-node27-storage-inventory-audit.timer`  | `03:40:00 UTC` daily | Audit reads the mover's committed final leaves and emits the completeness receipt. |
| 3     | `nhms-node27-resource-governance.timer`      | `04:10:00 UTC` daily | Governance audit reports the four new units + archive-root free-space band. |

### Cadence vs. retention-receipt validity window (design D6)

`#855` will pin the retention runner's completeness-receipt validity
window at 24 h. The storage inventory audit fires every 24 h with the audit
tick preceding every planned retention tick (audit at 03:40 UTC, retention
by construction after the audit), so a fresh, schema-valid
archive-completeness receipt is always present when the retention gate
consults it. **Do not lengthen the audit cadence beyond 24 h without
extending the retention receipt validity window first.**

TODO(#855): cross-link the retention runbook and the retention receipt
validity constant once the retention runner lands.

## Operation

### Reading receipts

- Product archive mover receipt:
  `/home/nwm/node27-product-archive-logs/receipt.json` (mode 0600).
  The receipt binds one `outcome`:
  - `success` — every selected candidate archived + verified + retired
    (enforce) or planned (dry-run).
  - `failed` / `indeterminate` — see per-candidate `terminals` and top-level
    `discovery_failures`.
  - `refused_free_space` — mover refused enforce because free space fell
    below the configured refuse watermark; sources untouched, `candidates`
    / `selected` / `deferred` / `terminals` / `events` /
    `discovery_failures` all empty. `free_space.band == "refuse"` and
    `free_space.free_bytes < free_space.refuse_bytes`. Non-zero exit.
- Storage inventory audit receipt (archive completeness):
  `/home/nwm/node27-storage-inventory-audit-logs/completeness-receipt.json`.
  Consumed byte-for-byte by the future retention gate (#855).

### Free-space watermark tuning

Initial values (in `infra/env/node27-product-archive.example`):

- `NHMS_ARCHIVE_FREE_SPACE_WARN_BYTES=322122547200` (300 GiB)
- `NHMS_ARCHIVE_FREE_SPACE_REFUSE_BYTES=161061273600` (150 GiB)

Config validation enforces `refuse < warn`, both `> 0`, both integer bytes.
If either env var is set, both must be set; empty/negative/non-integer
values fail closed at startup with no truthiness fallback.

Tune by watching the governance receipt `archive_root.band` field after
each daily tick:

- `clean` — `free_bytes >= warn_bytes` — steady state; no action.
- `warn` — `refuse_bytes <= free_bytes < warn_bytes` — review retention or
  archive backlog before the refuse gate fires. Governance receipt emits
  `ARCHIVE_FREE_BELOW_WARN` recommendation.
- `refuse` — `free_bytes < refuse_bytes` — mover WILL refuse next tick.
  Free space (drop retention, add capacity) before the next mover tick.
  Governance receipt emits `ARCHIVE_FREE_BELOW_REFUSE` recommendation.

If both watermark envs are unset, the mover runs without free-space
enforcement (backwards compatible) and the governance receipt reports
`archive_root.band = "unconfigured"`.

### Refuse-threshold behavior

When `free_bytes < refuse_bytes` at mover start:

1. Mover exits non-zero after publishing the receipt with
   `outcome = "refused_free_space"`.
2. No candidate discovery runs; no source is mutated; no staging tarball
   is created; the mover flock is released cleanly.
3. Receipt records exact measured `free_bytes`, configured `warn_bytes`
   and `refuse_bytes`, and `archive_root` path.

Dry-run mode still evaluates and reports the refusal terminal — the refuse
band is a governance signal, not a mutation-only gate.

## Rollback (unit-level, not data-level)

Both units are read-mostly (audit is read-only; mover's writes are already
gated by ADR 0002 "no deletion without archive receipt"). Rollback is
disabling the timers; the receipts stay on disk as historical evidence.

```
systemctl --user disable --now nhms-node27-product-archive.timer
systemctl --user disable --now nhms-node27-storage-inventory-audit.timer
```

Notes:

- Do **not** delete `~/node27-product-archive-logs/receipt.json` or
  `~/node27-storage-inventory-audit-logs/completeness-receipt.json`; they
  are the historical evidence chain and are consumed byte-for-byte by
  #855 retention.
- ADR 0002 makes archive+delete atomic per-cycle — there is no per-run
  data-level rollback because a completed archive terminal has already
  fsynced a verified pair before source retirement. To roll back a
  specific archived pair back to the object store, follow the salvage
  restore procedure in the (future) `db-export-salvage` runbook section
  or the archive rebuild drill (#5.x).
