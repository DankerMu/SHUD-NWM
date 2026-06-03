# Worklog: #270 + #272 — PRCP unit terminus hardening

## Goal

Close #270 and #272 in one PR. Both are regression-protection hardening surfaced by
the #266 review. Current code is already correct; this PR only adds assertions,
guard tests, and the OpenSpec change. No numeric/output behavior changes.

- #270: SHUD staging is the terminal consumer of the forcing `PRCP` column but never
  asserts its unit. Add a fail-loud terminus assertion that PRCP unit == `mm/day`.
- #272: producer output semantics are versioned only by a free-form
  `producer_version` string with no mechanical enforcement. Add a fingerprint guard
  pinning the semantics to the version, plus a keyset-equality guard.

## Boundaries (YAGNI)

- Do NOT change precipitation conversion numeric logic (#269 settled).
- Do NOT change `OUTPUT_UNITS` / `producer_version` values (only pin them).
- Do NOT add any new network fetch in staging — reuse the package manifest already
  fetched by `_verify_forcing_manifest_checksums`.
- Missing unit metadata is tolerated (backward compatibility); only an explicit
  non-`mm/day` PRCP unit is rejected.

## State machine (staging unit assertion — best-effort after round-1 fix)

```
package_manifest_uri present?
  no  -> skip (existing behavior, no assertion)
  yes -> checksum verified (existing)
         -> reread package manifest bytes (reused URI, capped 16MB)
            -> read fails / over-cap / invalid JSON?  -> stage (tolerate-skip)
            -> units block present?
                 no  -> stage (backward compat)
                 yes -> PRCP key present / non-None?
                          no  -> stage (backward compat)
                          yes -> PRCP == mm/day (strip/lower)?
                                   yes -> stage
                                   no  -> raise FORCING_PRCP_UNIT_MISMATCH  (ONLY hard failure)
```

## Progress

- [x] Confirmed package manifest `units` dict is readable at staging via
      `object_store.read_bytes_limited` (no new fetch); producer writes `units` from
      `OUTPUT_UNITS`.
- [x] `workers/shud_runtime/runtime.py`: added `EXPECTED_PRCP_UNIT`,
      `MAX_PACKAGE_MANIFEST_BYTES`, `_assert_forcing_prcp_unit`, wired into
      `_verify_forcing_manifest_checksums`; added `Mapping` import.
- [x] `tests/test_shud_runtime.py`: 3 #270 tests (mismatch / accept / tolerate
      missing); parametrized `_write_standard_shud_forcing(units=...)`.
- [x] `tests/test_forcing_producer.py`: fingerprint guard + precip-branch guard.
- [x] `tests/test_production_met_validation.py`: keyset-equality guard (placed next
      to the existing per-variable unit equality test).
- [x] OpenSpec change `prcp-unit-terminus-hardening`: proposal/design/tasks + specs
      (`shud-runtime` MODIFIED, `fixed-station-forcing-production` ADDED).
- [x] `uv run ruff check .` -> All checks passed.
- [x] `uv run pytest ...` local -> 135 passed; remote node-22 5fb0852 125 passed, 98be1eb 135 passed (EXIT=0).
- [x] `openspec validate prcp-unit-terminus-hardening --strict` -> valid (run locally; node not on remote PATH).
- [x] Round-1 review (3-pack): MED break-userspace — staging unit peek capped at 1MB hard-failed
      (FORCING_PACKAGE_MANIFEST_READ_FAILED), but the package manifest scales with station count
      (M23 QHH multi-station), so a >1MB manifest would brick a previously-runnable package.
- [x] Round-1 fix (98be1eb): unit peek made purely best-effort — read fail / over-cap (now 16MB) /
      invalid JSON / missing metadata all tolerate-skip; ONLY explicit non-mm/day raises. Removed
      READ_FAILED/INVALID codes. Tests: case/whitespace lock + unreadable/invalid-JSON tolerate.
- [x] Round-2 review (2-pack, comprehensive): CLEAN — MED eliminated (mutation-probed); zero
      in-scope CONFIRMED/blocking. Residual: LOW (no log on >16MB skip) -> YAGNI, not fixed.

## Notes / open observations

- Press is in `OUTPUT_UNITS` / `REQUIRED_FORCING_VARIABLES` / manifest metadata but
  not a SHUD CSV data column. Per ground-truth this is intentional metadata, not a
  bug — left untouched.
- The pinned fingerprint `8171ed72d3cbd29f034623b06220765ad46e7422a0851fb647871d54e6caf735`
  was computed from the current code and is recomputed identically inside the guard
  test; if the fingerprint construction in the test ever changes, regenerate it.
