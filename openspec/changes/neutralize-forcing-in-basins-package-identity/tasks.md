# Tasks

## 1. Packager identity material

- [ ] 1.1 Reduce `_forcing_checksum_material` (`workers/model_registry/basins_package.py:1466`) to `{"policy", "payload_copied"}` for both policies.
- [ ] 1.2 Remove `forcing_dir_original_name` from `source_material` (`basins_package.py:1294`).
- [ ] 1.3 Skip the per-file sha256 loop in `_forcing_metadata` (`basins_package.py:1155-1221`) when `copy_forcing=False`; keep `csv_count`/`byte_count` on `stat` alone and emit `aggregate_checksum: None`. Manifest evidence fields otherwise unchanged.
- [ ] 1.4 Bump `BASINS_PACKAGE_SCHEMA_VERSION`.
- [ ] 1.5 Confirm `copy_forcing=True` loses no identity coverage — forcing files remain `included_files` role entries hashed into `actual_checksum_material`; assert it in a test rather than in prose.

## 2. Discovery inventory

- [ ] 2.1 Stop emitting `forcing_csv_count` (`workers/model_registry/basins_discovery.py:291`) and drop the now-unused `_count_csv_files` call at `:239-249` if nothing else uses it.
- [ ] 2.2 Keep `forcing_dir`, `forcing_dir_original_name`, and forcing-related `quirks` — all have production readers or are ambiguity evidence.
- [ ] 2.3 Bump the inventory schema version; verify no validator pins the old value (`basins_registry_import.py:1908` compares manifest-vs-current-inventory at build time, so fresh publishes stay consistent — confirm, don't assume).

## 3. Production-closure reconstruction parity

- [ ] 3.1 Mirror the new material into `services/production_closure/object_store_validation.py:971-983`.
- [ ] 3.2 Branch `_package_checksum_from_stored_manifest` (`:870-905`) on the stored manifest's `schema_version`: pre-bump manifests reconstruct with the old seven-field shape, post-bump with the constant. Reuse the reconstruction-limitation plumbing at `:849-851` for anything undecidable.
- [ ] 3.3 Parity test asserting the packager's and the validator's `_forcing_checksum_material` agree on both schema generations.

## 4. Tests

- [ ] 4.1 Red test via re-discovery: publish with `forcing/` populated -> empty `forcing/` (directory kept) -> re-run discovery -> re-publish -> assert `content_sha256`, `source_sha256`, `package_checksum`, `source_inventory_checksum` all identical.
- [ ] 4.2 CSV-byte-mutation test: mutate a forcing CSV in place, re-discover, re-publish, assert the same four values unchanged.
- [ ] 4.3 Negative control: deleting the `forcing/` directory outright DOES change identity (structural change, correctly distinguishable from payload cleanup).
- [ ] 4.4 Historical-manifest reconstruction test: a stored pre-bump manifest still verifies its `package_checksum`.
- [ ] 4.5 Update the existing assertions that pin the old behavior — `tests/test_basins_package_publication.py:270-271,655-657`, `tests/test_basins_discovery.py` `forcing_csv_count` cases, `tests/test_basins_registry_import.py:2338`.

## 5. Contract and documentation

- [ ] 5.1 Spec deltas: `basins-asset-discovery` (inventory no longer carries the forcing CSV count) and `shud-model-package-publication` (declared-excluded forcing is not identity material).
- [ ] 5.2 ADR `docs/adr/0006-*.md`: the ruling, the rejected options B and C with reasons, and the one-time named churn.
- [ ] 5.3 Runbook: pin #1702 item 3's cleanup semantics — empty `forcing/`, do not remove the directory.

## 6. Issue closeout

- [ ] 6.1 Write the verified `dg_*` chain into #1813 (acceptance criterion 3).
- [ ] 6.2 Post the correction comment on #1702 overriding the 2026-08-22T20:02 "won't trigger cutover" conclusion (acceptance criterion 5).

## Evidence Floor

- `uv run ruff check .` clean.
- `openspec validate neutralize-forcing-in-basins-package-identity --strict --no-interactive` passes.
- `uv run pytest -q tests/test_basins_package_publication.py tests/test_basins_discovery.py tests/test_basins_registry_import.py tests/test_object_store_validation.py` — all pass, with the new tests in 4.1-4.4 present and asserting.
- `uv run pytest -q tests/test_scheduler_file_provider_refresh.py` — the cutover gate's existing fail-safe tests still pass unmodified.
- The 4.1 red test demonstrably fails on `master` and passes on the branch.
- No node-27/node-22 receipt required: this change is pure packaging-identity Python with no DB, display, or Slurm surface. Stated as a deliberate scope call, not an omission.
