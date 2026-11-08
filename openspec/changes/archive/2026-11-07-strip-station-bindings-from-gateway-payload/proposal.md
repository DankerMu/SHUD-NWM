## Why

#3095 removed `state_evidence` from every entry the orchestrator sends to the Slurm gateway. The node-22 receipt for that change (cycle `2026-10-16T00Z`, PR #3097 comment) shows what is left: a 65-task forecast index is still 5.95 MB, and 3.58 MB of it is `resource_profile.direct_grid_forcing`.

Almost all of that block is `station_bindings`, the basin's full direct-grid station binding table. `basins_shj` has 1140 rows, 313 KB of a 323 KB entry. The other `direct_grid_forcing` keys add up to under 1 KB. Every array stage (forcing, forecast, state_save_qc, parse) and publish sends the table again, and the gateway re-scans each request 5–6 times (#3095).

Nothing past the gateway boundary reads `station_bindings` from an entry (#3099, static audit):
- the gateway renders with its own `resource_profiles.yaml` profile;
- the sbatch templates pass only the index path and task id;
- the forcing producer reads the direct-grid contract from the registry file and the binding object at `binding_uri`;
- the SHUD runtime reads the per-run `manifest.json`, which is built from the unprojected basin and does not contain the table;
- `state_cli` falls back only to `resource_profile.package_checksum`;
- file-journal manual retry re-submits the index tasks as they are.

## What Changes

- **Projection.** `gateway_wire_entries` in `services/orchestrator/chain_manifests.py` also removes `station_bindings` and its alias `stations` from `resource_profile.direct_grid_forcing`. `packages/common/contracts/direct_grid.py:302` accepts both spellings.
  - Copy-on-write: only the entry, `resource_profile` and `direct_grid_forcing` mappings on the changed path are new; the orchestrator's basins, candidates and registry rows are not mutated.
  - Entries with no `resource_profile`, a non-mapping `resource_profile`, no `direct_grid_forcing` or a non-mapping `direct_grid_forcing` pass through unchanged.
- **Where it applies.** Unchanged from #3095: the array `tasks` (sent and written to `<stage>_manifest_index.json`), the stage manifest's `manifest_index`, and the publish `basins`.
- **Unchanged.** Every other `resource_profile` key, every other `direct_grid_forcing` key, the in-memory basins, `forcing_member_tasks`, and the per-run forecast runtime manifest.

## Triage

```text
Issue type: bugfix (perf)
Fixture level: expanded
Upstream suggested level: absent
Blast radius: a wire consumer that needed station_bindings breaks direct-grid forcing (none found); mutating shared dicts strips the table from in-process basins/registry and breaks forcing contract checks; dropping a sibling key breaks state_save_qc package_checksum fallback or direct-grid identity
Selected risk packs: Schema / field names (wire payload); Legacy compatibility; Resource limits; Concurrency / shared state (aliasing)
Evidence floor: uv run ruff check . ; targeted pytest ; node-27 targeted pytest ; node-22 receipt (index size + gateway submit latency vs 5.95 MB / 7–8 s)
```

## Impact

- **Code:** `services/orchestrator/chain_manifests.py` (`gateway_wire_entries` only).
- **Tests:** `tests/test_state_save_submit_ambiguity.py` (projection unit and wire capture).
- **Spec:** `slurm-job-chain` (adds a sibling requirement next to the #3095 state-evidence one).
