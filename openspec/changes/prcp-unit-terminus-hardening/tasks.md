## 1. SHUD staging terminus PRCP unit assertion (#270)

- [x] 1.1 Add `EXPECTED_PRCP_UNIT = "mm/day"` and `MAX_PACKAGE_MANIFEST_BYTES` constants in `workers/shud_runtime/runtime.py`.
- [x] 1.2 Add `_assert_forcing_prcp_unit(package_manifest_uri)` that rereads the already-checksum-verified package manifest, parses its `units` dict, and raises `SHUDRuntimeError("FORCING_PRCP_UNIT_MISMATCH", ...)` only when `units["PRCP"]` is explicitly present and `!= "mm/day"`.
- [x] 1.3 Tolerate missing `units` block / missing `PRCP` key (legacy packages) — no failure (backward compatibility).
- [x] 1.4 Invoke `_assert_forcing_prcp_unit` from `_verify_forcing_manifest_checksums` after the package checksum passes (no new network fetch).
- [x] 1.5 Surface read/parse errors as `FORCING_PACKAGE_MANIFEST_READ_FAILED` / `FORCING_PACKAGE_MANIFEST_INVALID`.

## 2. Producer output-semantics regression gate (#272)

- [x] 2.1 Add a fingerprint guard test pinning `OUTPUT_UNITS` + precip-branch behavior + `rn_shortwave_factor` default to `producer_version` (`m2.0`).
- [x] 2.2 Add a precip-branch test: `mm/day` -> `1.0`, any other unit raises `ForcingProductionError`.
- [x] 2.3 Add a keyset-equality guard: `set(OUTPUT_UNITS) == set(REQUIRED_FORCING_VARIABLES)` and every `package_manifest_unit(v)` non-empty.

## 3. Tests

- [x] 3.1 `tests/test_shud_runtime.py`: non-mm/day PRCP unit -> `FORCING_PRCP_UNIT_MISMATCH`.
- [x] 3.2 `tests/test_shud_runtime.py`: `mm/day` PRCP unit -> stages normally.
- [x] 3.3 `tests/test_shud_runtime.py`: missing unit metadata -> stages normally (backward compatibility).
- [x] 3.4 `tests/test_forcing_producer.py`: fingerprint + precip-branch guards.
- [x] 3.5 `tests/test_production_met_validation.py`: keyset-equality guard.

## 4. Verification

- [x] 4.1 `uv run ruff check .`
- [x] 4.2 `uv run pytest -q tests/test_forcing_producer.py tests/test_production_met_validation.py tests/test_shud_runtime.py`
- [ ] 4.3 `openspec validate prcp-unit-terminus-hardening --strict --no-interactive` (run by parent workflow)

### Evidence Floor

- SHUD staging fails loud with `FORCING_PRCP_UNIT_MISMATCH` when the forcing package explicitly declares a PRCP unit other than `mm/day`; observed and expected units appear in the message.
- Staging stages normally when the package declares `PRCP=mm/day`.
- Staging tolerates packages with no unit metadata (backward compatibility); no new network fetch is introduced (the package manifest fetch is reused).
- A producer guard test pins the output-semantics fingerprint to `producer_version`; changing OUTPUT_UNITS / precip-branch / `rn_shortwave_factor` turns it red.
- `set(OUTPUT_UNITS) == set(REQUIRED_FORCING_VARIABLES)` and every `package_manifest_unit(v)` is non-empty.
- No precipitation numeric logic or `OUTPUT_UNITS` / `producer_version` values changed.
- Required commands:
  - `uv run ruff check .`
  - `uv run pytest -q tests/test_forcing_producer.py tests/test_production_met_validation.py tests/test_shud_runtime.py`
  - `openspec validate prcp-unit-terminus-hardening --strict --no-interactive`
