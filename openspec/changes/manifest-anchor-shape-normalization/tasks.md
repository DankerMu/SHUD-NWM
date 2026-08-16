# Tasks: manifest-anchor-shape-normalization

## 1. Implementation

- [x] 1.1 Add a non-raising per-entry normalize-or-skip wrapper (reusing
      `_normalize_package_manifest_file_relative_path` verbatim; catches
      `SHUDRuntimeError` plus the bare `ValueError` that `urlparse` raises
      on a bracket-malformed authority — round-1 finding A) and apply it inside
      `_manifest_declared_shud_forcing_index_member` before the
      accepted-member intersection.
- [x] 1.2 Pass `forcing_uri` (`manifest["forcing"].get("forcing_uri")`) and
      `self.config.object_store_prefix` from the anchor call site in
      `_stage_standard_shud_forcing` into the anchor as arguments — both
      already in scope there; NO new parameters on
      `_prepare_shud_project_forcing`, NO `_ForcingPackageContext` change.
      Unavailable/empty forcing_uri → uri-derivation skipped per entry,
      dot-normalization still applied.
- [x] 1.3 Cover both declaration sources: package-manifest lane and
      run-manifest `_forcing_checksum_entries` fallback lane.
- [x] 1.4 Update the anchor docstring: shape tolerance, parser reuse,
      skip-not-raise contract.

## 2. Tests (tests/test_shud_runtime.py)

- [x] 2.1 Dot-prefixed legacy declaration + dual members on disk → legacy
      staged (package-manifest lane).
- [x] 2.2 Uri-only legacy declaration + dual members → legacy staged
      (package-manifest lane).
- [x] 2.3 Run-manifest fallback lane: dot-prefixed and uri-only shapes →
      legacy staged.
- [x] 2.4 Invalid/underivable entry → skipped, staging completes
      canonical-first, no raise.
- [x] 2.5 Both accepted members declared (mixed shapes) → anchor None →
      canonical-first (pin ambiguity semantics — note this FLIPS today's
      accidental legacy resolution, intended per spec delta). Extra case:
      duplicate same-member declarations (`shud/x` + `./shud/x`) → that
      member (zero delta vs today).
- [x] 2.6 Existing staging-level regression set untouched and green:
      tests/test_shud_runtime.py :1661/:1691/:1728/:1772/:1808/:1843 and
      DG pin `test_runtime_direct_grid_checksum_cap_uses_normalized_staged_tsd_path`
      (:2427). (No direct anchor-function tests exist today — do not tick
      this vacuously.)

## 3. Spec delta

- [x] 3.1 MODIFIED requirement delta in
      `specs/fixed-station-forcing-production/spec.md` (byte-faithful +
      appended sentence + 4 scenarios).

## 4. Evidence Floor

- [x] 4.1 `uv run pytest -q tests/test_shud_runtime.py` green (all new +
      existing).
- [x] 4.2 Red evidence: new shape tests fail against unmodified source
      (stash-based or pre-implementation run).
- [x] 4.3 `uv run ruff check .` passes (per issue Verification field).
- [x] 4.4 `openspec validate manifest-anchor-shape-normalization --strict
      --no-interactive` passes.
- [x] 4.5 Zero modifications to existing test assertions; DG lane and
      producer code untouched (diff inspection).
