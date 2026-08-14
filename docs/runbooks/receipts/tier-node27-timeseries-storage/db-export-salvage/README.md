---
status: archived
current_authority:
  - path: docs/runbooks/tier-node27-timeseries-storage.md
    section: "Retirement record: the cold archive lane is gone (2026-08-11)"
    reason: current node-27 timeseries storage-tier procedures
superseded_by: none
status_since: 2026-08-14
archive_scope: whole-document
retained_for: immutable live evidence of the retired archive lane
---

# Issue #1070 Task 3.3 live evidence

**Retired lane — read as history, not as procedure.** The node-27 cold archive
tier was permanently retired on 2026-08-11
([`docs/adr/0002-node27-timeseries-hot-cold-tiering.md`](../../../../adr/0002-node27-timeseries-hot-cold-tiering.md)
Revision 2026-08-11) after the `/dev/md0` double-disk failure, and #1370
deleted `scripts/node27_db_export_salvage.py` together with its wrapper, systemd unit and env template.
The receipts in this directory are immutable evidence of runs that happened;
they cannot be regenerated, and no command described below is runnable today.
Retention now deletes chunks with no archive backstop — see
[`docs/runbooks/tier-node27-timeseries-storage.md`](../../../tier-node27-timeseries-storage.md)
§8.

This directory preserves the immutable node-27 receipts for the explicitly authorized #1070 Task 3.3 live database-export salvage run on 2026-07-15.

The run was bound to the frozen 228-selector input and executed once under the
read-only `nhms_display_ro` database role. It performed no database writes;
mutations were limited to archive-side export objects and manifests. The
enforce run exported 228/228 selectors, and independent verification passed
for all 228 objects and manifests: 75,922,800 rows and 1,065,525,623 compressed
bytes. The fresh follow-up inventory classified 1,585/1,585 windows as complete
and returned an empty salvage list. Retention, the compression drill, and
node-22 business scheduling were outside this task's authorization and were not
executed.

## Receipts

- `stream-preflight-live-20260715T074343Z.json` — `c3ea7902d958ef439ed359dfe126d6e28cd169d1a2c7c74109a85c3310d8adb6`
- `salvage-dryrun-live-20260715T074343Z.json` — `4ffc166daa16de563b81d2c5c39092a4c2e7478f3d3de3d0dfbbde2cb668ee3d`
- `preflight-dryrun-envelope-live-20260715T074343Z.json` — `27ee230b8f72742e3294154d0fd568993e3c1c5e1c28979234fefc4aa92c9522`
- `salvage-enforce-live-20260715T074343Z.json` — `11237018decbb0768dba7c6f59d90a1bee741f48b82f847780b5a03ea29e09bb`
- `post-enforce-object-verification-live-20260715T074343Z.json` — `8e4f963e51e03e856c268e1a68ed4ad0ca80dcc9f4db59b82f3340b8c6bbb9ab`
- `completeness-followup-live-20260715T075054Z.json` — `2277c617900a62f5eca1253ff967650da6790b5952bd4658c10eb1a6d281bb54`
- `terminal-receipt-live-20260715T075054Z.json` — `01f6256203530704840ce528d03fe2ef1c4939b05e50e1788dfefc98dc24e767`
