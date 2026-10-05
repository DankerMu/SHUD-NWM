# Provision is dry-run by default and leaves a succession receipt

Issue: #2737 (part A of #1815). Fixture level: **compact**. Risk packs: **DB write path / idempotency**,
**Operator contract (CLI + receipt)**, **Legacy compatibility**.

## Why

`scripts/provision_direct_grid_scheduler_registry.py` is the only step of a model succession that writes the
database (`core.model_instance` INSERT / UPDATE, `met.met_station` upsert). It also builds the variant package
on the object store and replaces `--output-registry`. It has no preview: the first thing an operator learns
about a run is what it committed. Its `--output` report is overwritten in place. `provision_direct_grid_registry`
has no test against a fake database.

The succession chain (#1815) needs each step to refuse when the previous step's receipt is missing, with the
receipts on the NFS both nodes share. This change gives the first step its receipt.

## What changes

1. **Dry-run is the default; `--apply` writes.** Without `--apply` the script performs every read and
   validation and predicts each variant's `model_id`, and it does not: write the database, create or modify
   (including chmod) any file under the object store other than its own receipt under `--receipt-root`, or
   write `--output-registry`. The first output line of a dry-run says that nothing was written and that an
   apply always writes (see 3).
2. **How the dry-run predicts `model_id`.** A variant's `model_id` is
   `_mint_model_id(basin_version_id, canonical_grid_key, model_input_package_id, binding_checksum)`
   (`workers/model_registry/direct_grid_variant_registration.py`), and `binding_checksum` exists only in a built
   package. So:
   - package already on the object store (`manifest.json` present): read its contract; do not chmod it;
   - package absent: build it into a temporary directory, read the contract, delete the directory.
   Deviation from #2737's text ("不构建包"): the identity cannot be computed without a build; the build happens
   outside the object store and is discarded.
   The temporary directory is created under `--build-tmp-dir` (default `tempfile.gettempdir()`, i.e. `TMPDIR`);
   the tool refuses a location that resolves inside the object-store root. Each variant's directory is removed
   in a `finally` block before the next variant is built, including on failure.
   The registration module exposes one read-only function (suggested name `plan_direct_grid_variant`) that
   returns `(model_id, would_insert)` and shares validation, snapshot resolution, lookup and id derivation
   with `register_direct_grid_variant`; the script does not copy the id derivation.
   A dry-run issues exactly these statements: `_load_snapshot` (two SELECTs per source grid),
   `_baseline_db_inputs`, `_resolve_snapshot`, `_lookup_existing_variant`. Its connection is opened read-only
   (`connection.set_session(readonly=True)`) and rolled back, never committed.
   If a temporary build does not reproduce the final package's `binding_checksum`, stop and report: this
   fixture assumes it does (the binding bytes hold no path, timestamp or host), and a test pins it.
3. **`inserted = false` does not mean an apply writes nothing.** An apply always updates the variant row's
   `model_package_uri` / `resource_profile` and upserts one `met.met_station` row per station, on the insert
   path and on the reuse path alike. The receipt records `station_count` as the number of mirror upserts an
   apply will issue (known after the build, so never null). A dry-run does not predict a mirror collision
   (`DIRECT_GRID_VARIANT_MIRROR_COLLISION`); that can still fail an apply, which rolls back.
4. **Succession receipt.** New options `--succession-id` (required with `--apply`; `[A-Za-z0-9._-]{1,80}`)
   and `--receipt-root` (default `<object-store-root>/scheduler/succession`). A run given a succession id
   writes one receipt with `O_WRONLY|O_CREAT|O_EXCL`, mode 0644:
   `<receipt-root>/<succession-id>/provision-dry-run.json` or `.../provision-apply.json`. A dry-run without
   `--succession-id` writes no receipt and prints the plan to stdout. Schema
   `nhms.model_succession.provision_receipt.v1`:
   - `succession_id`, `step` (`"provision"`), `dry_run` (bool), `outcome` (`"planned"` | `"applied"`),
     `generated_at`, `operator_id`, `host`, `git_commit` (of the checkout, null when unavailable);
   - `object_store_root`, `object_store_prefix`, `selected_model_ids[]`;
   - `baseline_registry` and `output_registry`: `path` as given, `object_store_key` (the path relative to the
     object-store root, null when outside it), `sha256` (`output_registry.sha256` is null in a dry-run);
   - `source_grids[]`: `source_id`, `grid_id`, `grid_snapshot_id`, `grid_signature`, `canonical_grid_key`;
   - `models[]`: `baseline_model_id`, `model_id`, `source_id`, `grid_id`, `basin_version_id`, `package_key`
     (`models/direct_grid_variants/<baseline>/dg-<src>-<identity>/package`), `model_package_uri`,
     `manifest_uri`, `package_checksum` (sha256 of `manifest.json`; also given for a temporary build),
     `inserted` (dry-run: the predicted value), `package_prebuilt` (bool), `station_count`;
   - apply only: `dry_run_receipt` (`path`, `object_store_key`, `sha256`).
   Paths are recorded as seen from the host that ran the step; `object_store_key` is what the other node
   resolves. The provision step knows baseline -> variant, not predecessor -> successor; a later step finds the
   predecessor from the canonical manifest by `(basin_version_id, source_id)`.
   Before any build or database statement the tool checks that the target receipt path does not exist and that
   the receipt directory can be created and written; either failure exits non-zero having done nothing.
   `O_EXCL` at write time remains as the race guard. An unwritable root is refused with a message naming the
   path and the one-time setup (`frd_muziyao` on node-22 creates `scheduler/succession/`, group `nwmuser`,
   mode 2775). The script never changes permissions on the receipt root.
5. **Apply requires its dry-run.** `--apply` refuses unless `provision-dry-run.json` exists under the same
   `succession-id` (the message names the expected path), has the same `baseline_registry.sha256`,
   `source_grids` (all five fields), `object_store_prefix`, `output_registry` path and `selected_model_ids`,
   and the set of `(baseline_model_id, source_id, model_id)` it predicted equals the set this run registers.
   `operator_id` is not compared. Each variant is compared right after its contract is read and before it is
   registered, inside the database transaction: on a mismatch the transaction rolls back, no registry is
   published, no apply receipt is written, and the exit is non-zero with both values printed. A package built
   on the object store before the mismatch was detected stays (with its `direct_grid_build_receipt.json` and
   the readable-mode chmod); the message says so.
6. **Receipt before success, not instead of it.** The apply receipt is written after the registry publish. If
   the receipt write fails, the exit is non-zero and the message states that the database and registry were
   written and the receipt was not. A retry then needs a new succession id and its own dry-run, and its
   receipt will say `inserted = false`.
7. **`--output`** in a dry-run is written with `"status": "planned"`; with `--apply` it is unchanged.
8. **Runbooks** (`docs/runbooks/production-ops/service-bringup.md` hop 3,
   `docs/runbooks/production-ops/recalibration-and-archive.md` section 5.7.1): both describe this step in
   prose only; add the command sequence (dry-run, read the receipt, `--apply`). Refer to code by symbol name,
   not `file.py:NNN`, and replace the two existing line-number references to this script with symbol names.

## Must preserve

- With `--apply`, the database write statements, their order, the single transaction, the package layout, the
  registry rows, and the `--output` report fields are what they are today. The only added statements are the
  two read-only SELECTs of the planning function per variant, issued before that variant's first write so the
  predicted `model_id` can be compared with the dry-run receipt. `--output` keeps its current
  overwrite behaviour; the receipt is the new durable artifact.
- Re-running an applied provision is idempotent on the database and the object store as today
  (`inserted = false`, package build skipped); it needs a new succession id and its own dry-run.
- The baseline-only input check, the missing `--model-id` check, and the missing
  `DATABASE_URL` / `OBJECT_STORE_ROOT` / `OBJECT_STORE_PREFIX` check.
- `scripts/node22_clone_direct_grid_cutover_states.py` keeps importing `_category_files` and
  `_required_single` from this module.
- `register_direct_grid_variant`'s behaviour and signature.
- `model_id` immutability: no option overrides or supplies a `model_id`.

## Behaviour change to call out

A command line that worked yesterday without `--apply` now writes nothing. That is the point of the change
and the reason the runbooks are updated in the same PR. The script's first output line in a dry-run says so.

## Out of scope

- The merged manifest publish (#2738), the node-22 orchestration and timer (#2739), cold start (#2740),
  adding and removing basins (#2741).
- Copying the variant package from NFS to node-22 scratch.
- The `met.met_station` upsert inside registration (its own `ON CONFLICT DO UPDATE`).

## Required evidence

Tests use the keliya fixture plus stubbed category files (as `tests/test_mapping_builder_cli.py` does), a
recording fake connection owned by the new suite, and `tmp_path`.

- Dry-run: the connection saw only the SELECT statements listed in 2, was set read-only and never committed;
  no file was created or modified under the object-store root except the receipt (one case with the receipt
  root inside the object-store root, the production layout); nothing at `--output-registry`; the temporary
  build directory is gone, also when the build raises; a prebuilt package's file modes and mtimes are
  unchanged. Cases: a not-yet-built variant, and a prebuilt, already-registered one.
- `--build-tmp-dir` inside the object-store root is refused.
- The dry-run's predicted `model_id` and `package_checksum` equal what an apply of the same inputs then
  registers (the test of the temporary-build assumption).
- Apply: INSERT path and reuse path; statements and order pinned.
- Apply refusals, each naming the expected dry-run receipt path where it is missing: no dry-run receipt;
  different baseline registry; different source grid snapshot; predicted set differs (transaction rolled back,
  registry not published, no apply receipt).
- Receipt: a second run with the same id and mode fails before any database statement or build and leaves the
  first receipt byte-identical; an unwritable root is refused with the setup message before anything else;
  a dry-run without `--succession-id` writes no receipt.
- `uv run ruff check .`; targeted pytest; the selector picks the new suite for a diff of the script and of
  the registration module.
- node-27, read-only: one real dry-run against production for an already-provisioned model (it must predict
  the existing `model_id` with `inserted = false` and write nothing). No `--apply` on node-27 in this change.
  Not verifiable offline: whether node-27 sets `idle_in_transaction_session_timeout`, and the size of a
  variant package in the temporary directory.
