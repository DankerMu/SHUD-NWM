# Design: PRCP unit terminus hardening

## Decision: assert at the staging terminus, reuse already-fetched metadata

SHUD is the terminal consumer of the forcing `PRCP` column. The authoritative unit
is `mm/day` (Decision A). The converter and producer already assert/normalize this
upstream, but staging never re-checks it. We add a fail-loud terminus assertion so
that any future upstream regression that re-introduces per-step `mm` is caught
before SHUD ingests a wrong precipitation magnitude.

## Ground-truth evidence

### The unit metadata is already available at staging

`prepare_workspace` calls `_verify_forcing_manifest_checksums` (`runtime.py`),
which, when `forcing.package_manifest_uri` is present, already:

1. computes and verifies the package manifest checksum, then
2. (new) reads the same package manifest bytes via
   `object_store.read_bytes_limited(...)` and parses its `units` dict.

The producer writes that `units` dict into the package manifest
(`workers/forcing_producer/producer.py`, `"units": units` where
`units = {v: OUTPUT_UNITS[v] ...}`), so `units["PRCP"] == "mm/day"` for any package
produced by the current code. No new network round-trip is introduced; we reread
the same URI the checksum step already touched.

### Backward-compatibility philosophy (mirror existing code)

`_verify_forcing_manifest_checksums` already treats a missing
`package_manifest_uri` as "skip" (only fails when URI and checksum are
half-present). The new assertion mirrors that: if `units` is absent, or
`units["PRCP"]` is absent, staging proceeds (legacy packages). Only an
**explicitly present** PRCP unit `!= "mm/day"` raises
`FORCING_PRCP_UNIT_MISMATCH`. This matches the project-wide "reject explicit
mismatch, tolerate missing metadata" stance.

## Failure modes and error codes

| Condition | Behavior |
|-----------|----------|
| `units["PRCP"] == "mm/day"` | stage normally |
| `units["PRCP"]` present, `!= "mm/day"` (e.g. `"mm"`) | raise `FORCING_PRCP_UNIT_MISMATCH` (message carries observed + expected) |
| `units` block absent / `PRCP` key absent | tolerate, stage normally |
| package manifest unreadable | `FORCING_PACKAGE_MANIFEST_READ_FAILED` |
| package manifest not valid JSON | `FORCING_PACKAGE_MANIFEST_INVALID` |

## Producer regression gate (#272)

### Output-semantics fingerprint pins producer_version

A guard test computes a stable SHA-256 over the producer's observable output
semantics:

- `repr(sorted(OUTPUT_UNITS.items()))`
- precip branch behavior: `_precip_to_timestep_factor` returns `1.0` for `mm/day`
  and raises for any other unit
- `rn_shortwave_factor` default

and asserts `(fingerprint, producer_version) == (EXPECTED_FINGERPRINT, "m2.0")`.
Changing any output semantic flips the fingerprint -> the test goes red -> the
developer must bump `producer_version` AND update the pinned fingerprint in the
same change. This is the enforcement gate keeping #266 lineage currency honest.

### Keyset lockstep

`set(OUTPUT_UNITS) == set(REQUIRED_FORCING_VARIABLES)` plus a non-empty
`package_manifest_unit(v)` for every required variable. The pre-existing
per-variable equality test only iterates `REQUIRED_FORCING_VARIABLES`, so a stray
OUTPUT_UNITS key would slip through; the set-equality guard closes that gap.

## Alternatives rejected

- **Inject units into the run manifest and assert there**: would require a
  producer change to write units into the run manifest. Rejected as out of scope;
  the package manifest already carries units and is already fetched.
- **Parse units from the SHUD CSV header**: the SHUD CSV column header has no unit
  annotation; only the package manifest is authoritative.
