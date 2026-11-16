# synthetic-package/ — M1 model asset package for the rehearsal

Evidence-only synthetic model asset package for the display-cutover
rehearsal's M1 target. Byte-stable and hand-derived (no build tooling
involved) so the checksums are reread from these exact bytes at Phase B.

## Layout

```
synthetic-package/
  README.md                          (this file)
  package/
    synth-basin-m1-v2.mesh           (minimal SHUD mesh placeholder, non-empty)
    synth-basin-m1-v2.para           (minimal SHUD parameter placeholder, non-empty)
    synth-basin-m1-v2.calib          (minimal SHUD calibration placeholder, non-empty)
    binding-manifest.json            (§7.2 direct-grid contract for the M1 target)
    package.manifest.sha256          (aggregate reread checksum evidence)
```

The three sidecar files (`.mesh`, `.para`, `.calib`) are the required
suffixes `workers/model_registry/validator.py` checks (`REQUIRED_SUFFIXES`
at line 9). They are non-empty; content is a single evidence-marker line
each. Only their SUFFIX presence matters to the validator; content is not
inspected.

## Purpose

The rehearsal's Change 4 `activate` preflight (`_activation_safety_evidence`
in `packages/common/model_registry.py:3773-3814`) validates the M1 target's:

- `model_package_uri` — a non-empty https scheme URI (points at this
  synthetic-package/ subtree on GitHub);
- `resource_profile.package_checksum` + `.package_checksum_verified=true`
  (patched by `provisioning/02-register-direct-grid-variant.py` at
  registration time; the checksum literal matches the one recorded in
  `package.manifest.sha256` here);
- `resource_profile.copied_root_status='present'` (patched at registration).

The package files themselves are NOT read by the running SHUD binary in
this rehearsal (no basin activation → no scheduler dispatch → no forcing
producer run). They exist to give the URI + checksum a stable byte-level
anchor for the audit trail.

## Rehearsal-only, not a production package

This package is NOT interchangeable with a real basin's model package. It
carries no real `.sp.att`, no forcing CSVs, no runnable mesh. Do not
copy this into any real basin's package tree.

## Contract manifest reference

The `binding-manifest.json` file mirrors the direct-grid contract passed
to `register_direct_grid_variant` by
`provisioning/02-register-direct-grid-variant.py`. It is checked-in evidence
for the exact station-binding identity the flip hook re-points onto:

- `model_input_package_id = "synth-mip-m1-v2"` (M1 mapping-asset identity)
- `binding_checksum = "d1e2c3b4a5968778869574a3b2c1d0e9f8a7b6c5d4e3f2a1b0c9d8e7f6a5b4c3"`
- `grid_id = "synth-grid-p0.2-m1-v2"`
- 3 station bindings, station_ids following the
  `<mapping_asset_identity>::cell:<grid_cell_id>` convention
  (`workers/mapping_builder/binding.py:1501` — `STATION_ID_SEPARATOR="::cell:"`)
