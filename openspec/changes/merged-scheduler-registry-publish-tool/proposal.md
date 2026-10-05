# A checked-in tool publishes the merged scheduler registry manifest

Issue: #2738 (part B of #1815). Fixture level: **compact**. Risk packs: **Production control-plane file write**,
**Identity fail-closed**, **Partial-failure recovery**.

## Why

On direct-grid production the only way to change the model set is to publish a merged registry manifest: the
current canonical rows, minus the rows being retired, plus rows produced by
`scripts/provision_direct_grid_scheduler_registry.py`. No checked-in tool does it. The 2026-08-22 and
2026-08-25 changes used ad-hoc scripts that called `publish_scheduler_registry_manifest`; the runbooks
(`recalibration-and-archive.md` section 5.7.1, `service-bringup.md` hop 4, `operating-scope.md` section 7.2)
carry a prose recipe with hand-run checks. Model succession is frequent, and every kind (recalibration,
structural change, adding or removing a basin) goes through this step.

Read on 2026-10-05 from the production canonical manifest (read-only): 132 rows, 66 `basin_id`s, each with
exactly one `gfs` and one `IFS` row, all `direct_grid`, 13,353,454 bytes and 329,431 JSON nodes against the
registry bounds (`MAX_REGISTRY_MANIFEST_BYTES`, `MAX_REGISTRY_MANIFEST_JSON_NODES`; 32 MiB / 800,000 since
#2744).

## What changes

1. New node-22 tool `scripts/node22_publish_merged_scheduler_registry.py`, DB-free, with its logic in an
   importable function (part C of #1815 will call it). It is a dry-run unless `--apply`. Runbooks invoke it as
   `cd /scratch/frd_muziyao/NWM && .venv/bin/python -m scripts.node22_publish_merged_scheduler_registry`.

2. Inputs:
   - the canonical manifest (`NHMS_SCHEDULER_REGISTRY_MANIFEST`) and the worker mirror
     (`NHMS_SLURM_SCHEDULER_REGISTRY_MANIFEST`), each overridable by an option;
   - roots: `OBJECT_STORE_ROOT` (compute object store; contains the mirror) and
     `NHMS_SCHEDULER_PROVIDER_STORE_ROOT` (shared store; contains the canonical manifest and the default
     receipt root), plus `OBJECT_STORE_PREFIX`. Both publishes pass `object_store_root=OBJECT_STORE_ROOT`,
     `object_store_prefix=OBJECT_STORE_PREFIX`, `require_direct_grid=True`, as
     `scripts/scheduler_refresh/runner.py` does for the canonical one; the provider store root is used only as
     the containment root of the canonical path. The tool refuses when any libpq / database variable is set;
   - operations, each repeatable: `--replace <old_model_id>:<new_model_id>`, `--add <new_model_id>`,
     `--remove <model_id>`. At least one is required;
   - `--operator-id` (required); the provider refresh lock path from `NHMS_SCHEDULER_PROVIDER_REFRESH_LOCK`
     (needed by `--apply`);
   - `--succession-id` (same rules as the provision step), `--provision-succession-id` (default: the
     succession id) and `--receipt-root` (default `<provider store root>/scheduler/succession`);
   - `--new-rows-registry <path>`: the registry file written by the provision `--apply`. Optional when the
     provision apply receipt records an `object_store_key` for its `output_registry`, in which case the path is
     resolved against the provider store root. It is read as plain JSON (schema version and embedded checksum
     checked, no freshness check), not through `FileSchedulerModelRegistry`, so rows stay verbatim.

3. New rows come only from a provisioned registry. When any `--replace` or `--add` is given, the tool requires
   `<receipt-root>/<provision-succession-id>/provision-apply.json` (schema
   `nhms.model_succession.provision_receipt.v1`, `step = provision`, `dry_run = false`, `outcome = applied`,
   matching `succession_id`); the sha256 of the new-rows registry file must equal that receipt's
   `output_registry.sha256`; every new `model_id` named by the operations must be in the receipt's
   `models[].model_id`; and every receipt `model_id` must be either introduced by an operation or already
   present in the canonical manifest (reported as `already_published_model_ids`). A provisioned id that is
   neither is a refusal naming it. A remove-only run needs no provision receipt.

4. The merged list is: canonical rows in their current order, as the JSON objects read from the file (not
   re-normalised); a replaced row is substituted in place by the new row taken verbatim from the new-rows
   registry; removed rows are dropped; added rows are appended in the order given.

5. Checks, all before any write, each a refusal with a message that names the offending ids:
   - canonical and mirror are byte-identical before the change (otherwise: run the provider refresh first);
   - every `old_model_id` / removed `model_id` is in the canonical manifest; every new `model_id` is in the
     new-rows registry and not already in the canonical manifest; no id appears in two operations;
   - a row (canonical or new) without `resource_profile.direct_grid_source_id`, or whose normalised value is
     not in the `applicable_source_ids` of its `direct_grid_forcing` contract, is a refusal naming the
     `model_id`;
   - a replace keeps `basin_id` and the source (`resource_profile.direct_grid_source_id`, compared after
     `normalize_source_id`); `basin_version_id` may change and the receipt records old and new;
   - in the merged list `model_id` is unique, `(basin_id, source)` is unique, every row is direct-grid, and
     every `basin_id` has exactly one row for each source of the set of sources present in the canonical
     manifest before the change (so adding one source of a basin, or removing one, is refused);
   - row count equals `before + adds - removes`;
   - every new row's `manifest_uri` exists and matches its `package_checksum` under `OBJECT_STORE_ROOT` (what
     the publisher checks) and under `NHMS_SCHEDULER_PROVIDER_STORE_ROOT`, so a missing copyback is refused
     before the canonical write;
   - both manifests and their directories satisfy the atomic writer's ownership and mode rules (checked up
     front so the mirror cannot fail on them after the canonical write);
   - the merged manifest passes the publisher's own validation, byte cap and JSON node cap. The dry-run
     establishes this, and the predicted size, by calling the real `publish_scheduler_registry_manifest` on a
     file in a private temporary directory outside both manifest directories and deleting it.
   A dry-run takes no lock in and creates no file in either manifest directory.

6. Dry-run does all of 3-5, writes neither manifest and no backup, and reports: operations, row counts before
   and after, `introduced_model_ids`, `removed_model_ids`, `already_published_model_ids`, per replaced pair
   the old and new `basin_version_id`, predicted manifest bytes and JSON nodes with their caps and the
   remainder, and the sha256 of both manifests as read. With `--succession-id` it writes
   `publish-dry-run.json`.

7. `--apply` requires `--succession-id` and that id's `publish-dry-run.json`, and refuses unless the dry-run
   recorded the same operations, the same sha256 of the canonical `models` array (canonical JSON of the array;
   a pure renewal that only changes `generated_at` does not invalidate the dry-run) and the same merged
   `model_id` list. For the whole apply it holds the provider refresh lock (the one
   `scripts/scheduler_refresh/runner.py` takes) without blocking; a held lock is a refusal. Then, in order:
   1. back up both manifests to `<manifest>.bak-<succession-id>-<UTC yyyymmddThhmmssZ>` (exclusive create),
      writing the very bytes of the snapshot whose preimage is used for the compare-and-swap;
   2. publish the canonical manifest with `publish_scheduler_registry_manifest` and that `expected_preimage`
      (registry preimages are captured with the registry byte cap, not the function's default);
   3. publish the mirror with the same rows, the same `generated_at` and its own `expected_preimage`;
   4. read both back and require one sha256.
   The tool does not stop or start any timer or service and does not run the provider refresh.

8. Failure from step 2 onward. Each publish passes `commit_observer` and keeps the committed
   `ProviderPreimage`. After any exception or read-back mismatch the tool re-reads both manifests. A
   destination whose bytes still equal what was read is left alone. A destination this run committed is
   restored to the bytes read with the atomic provider writer and `expected_preimage = <its committed
   preimage>` (as `_restore_provider_path` in `scripts/scheduler_refresh/providers.py`); it is never restored
   against a re-captured preimage. Outcomes: both equal the bytes read and nothing was committed -> `refused`;
   the canonical compare-and-swap refused the first write (another writer changed it; this run committed
   nothing) -> `refused`, with the current sha256 recorded; both back at the bytes read after this run
   committed -> `rolled_back`; anything else (restore failed, changed without a commit token, changed by
   another writer after a commit) -> `inconsistent`, with both current sha256, both
   backup paths and the two ways out (restore both from backup, or publish the mirror). Exit is non-zero in
   all three. If both manifests were published and read back equal but the apply receipt cannot be written,
   nothing is rolled back; the tool exits non-zero and prints the sha256, both backup paths,
   `manifest_generated_at` and that the publish is complete but unreceipted.

9. Receipts, schema `nhms.model_succession.publish_receipt.v1`, exclusive create, never overwritten; the
   receipt directory is checked before any backup or write (reuse the helpers of
   `packages/common/provision_succession_receipt.py`; move what is generic rather than copying it, keeping the
   existing import path working). A succession directory created by either step is group-writable, so the
   other node's user (same group) can add its receipt.
   - `publish-dry-run.json`: one per succession id; a changed plan needs a new succession id (with
     `--provision-succession-id` naming the unchanged provision).
   - `publish-apply.json`: written only with `outcome = published`; its existence refuses any further apply of
     that id.
   - `publish-apply-failed-<same UTC stamp as the backups>.json`: every apply attempt that got past the
     pre-write checks and did not publish. The same succession id may be retried while no `publish-apply.json`
     exists. A refusal before any backup writes no receipt.
   Fields: `succession_id`, `provision_succession_id`, `step = publish`, `dry_run`, `outcome` (`planned` |
   `published` | `refused` | `rolled_back` | `inconsistent`), `generated_at` (receipt time), `operator_id`,
   `host`, `git_commit`, `operations`, `row_count_before`, `row_count_after`, `merged_model_ids`,
   `introduced_model_ids`,
   `removed_model_ids`, `already_published_model_ids`, `replaced[]` (`old_model_id`, `new_model_id`,
   `basin_id`, `source_id`, `old_basin_version_id`, `new_basin_version_id`), `canonical` and `mirror` (each
   `path`, `sha256_before`, `sha256_after`, `backup_path`), `canonical_models_sha256_before`,
   `manifest_generated_at`, `manifest_bytes`, `manifest_bytes_limit`, `manifest_json_nodes`,
   `manifest_json_nodes_limit`, `provision_apply_receipt` (`path`, `sha256`) when used, `dry_run_receipt` on
   apply, `reason` when not successful.

10. Runbooks: the three prose recipes are replaced by the tool's command sequence (dry-run, read the receipt,
    `--apply`). The text keeps the order constraint (provision, clone rows, then publish), says the scheduler
    timer should be stopped around the apply and that a canonical/mirror mismatch makes the workers refuse to
    submit, keeps the manual provider refresh as the step after the publish, and states the measured manifest
    size against the caps. The heading of section 5.7.1 keeps its text (other documents link to its anchor).

## Must preserve

- `publish_scheduler_registry_manifest`, the atomic provider writer, the provider refresh and the provision
  script keep their behaviour; receipt files written by the provision step keep their names, schema and fields.
- Rows that no operation names are published as the same JSON objects, in the same order.
- The tool refuses when a database variable is set, opens no database connection, and neither it nor the
  shared receipt helpers import a database driver themselves. (The publisher's own import chain loads
  `psycopg2` through `packages/common/redaction.py`; removing that is not part of this change.)
- `model_id` immutability is not relaxed: a row is only ever replaced by a row with a different `model_id`
  taken from a provisioned registry; the tool never edits a row.
- The code runs on Python 3.11 and 3.12.

## Out of scope

Stopping or starting the scheduler timer, the copyback, the state clone, the provider refresh, and the
ordering gate against the clone receipt (part C). Cold start (part D). Database changes for adding or
removing a basin (part E). Raising the manifest size cap.

## Required evidence

Tests use temporary directories, the real `publish_scheduler_registry_manifest`, and fake packages present
under both roots.

- replace, add, remove and a mixed run publish the expected rows; untouched rows are the same objects in the
  same order; both files are byte-identical with one `generated_at`; backups hold the previous bytes.
- every refusal of 3 and 5 has a case, and in each both manifests are unchanged and no backup exists.
- dry-run changes no file other than its receipt.
- compare-and-swap: the canonical manifest changed between read and publish -> refused, both unchanged.
- mirror publish fails after the canonical write -> canonical restored, `rolled_back`; the read-back differs
  -> both restored; the mirror publish raises after its commit -> the mirror is restored too; at restore time
  the canonical manifest was rewritten by someone else -> it is not overwritten and the outcome is
  `inconsistent` with both backup paths. Each failed attempt leaves a `publish-apply-failed-*.json` and no
  `publish-apply.json`, and a retry with the same id then succeeds.
- both published but the apply receipt cannot be written -> nothing rolled back, non-zero exit.
- a renewal that changes only `generated_at` between dry-run and apply does not block the apply; a change of
  the canonical `models` does.
- a provisioned id that is neither introduced nor already published -> refused; a batch in which one
  provisioned id is already published -> accepted and reported.
- a row without `direct_grid_source_id`, or with one outside its contract's `applicable_source_ids` -> refused.
- a held provider refresh lock -> refused before any backup.
- a dry-run leaves no lock file and no other new file in either manifest directory.
- apply without the dry-run receipt, with different operations, or after the canonical manifest changed since
  the dry-run -> refused before any backup.
- an existing `publish-apply.json` or `publish-dry-run.json` -> refused before any write.
- the tool and the shared receipt helpers contain no database driver import; importing the tool loads no
  driver module beyond what the publisher stack loads; a database variable in the environment is a refusal.
