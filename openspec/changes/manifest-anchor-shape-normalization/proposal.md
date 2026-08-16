# Proposal: manifest-anchor-shape-normalization

## Why

Issue #1357 (PR #1354 round-3 T1+U3 DEFER): the non-direct-grid staging
anchor `_manifest_declared_shud_forcing_index_member` matches package-manifest
`files[].relative_path` by bare string equality, while the direct-grid lane
normalizes the SAME `forcing_package.json` before use (`./` prefix eaten by
`PurePosixPath(...).as_posix()`; missing `relative_path` derived from `uri`
relative to `forcing_uri`). A `./`-prefixed or uri-only declaration therefore
returns `None` from the anchor and the dual-member tree silently falls back
canonical-first — consuming exactly the stale canonical orphan the anchor
(design D4-2 of #1176) exists to avoid, with no error and no log difference.
Verifier probe (round-3): legacy declared with `./` prefix or uri-only +
dual members on disk → run succeeds consuming the stale orphan.

Reachability today is producer-enumeration-negative (the only production
writer emits plain `relative_path`, checksum-pinned), so this is hardening —
but `forcing_package.json` has no JSON Schema, lives in the object store
(may predate repo history), and the DG lane DELIBERATELY tolerates both
shapes: contract-legal input must not silently degrade.

## What Changes

- `workers/shud_runtime/runtime.py`: the anchor normalizes each entry with
  the SAME rules as the DG lane before the accepted-member intersection,
  via a non-raising derive-or-skip wrapper around
  `_normalize_package_manifest_file_relative_path` (invalid/underivable
  entries are skipped — the anchor is a best-effort resolver, and the
  non-direct-grid lane MUST NOT gain a new fail-closed surface).
- Context sourcing: `forcing_uri` + `object_store_prefix` (the same sources
  the DG lane's normalize call site uses) are already in scope at the
  anchor call site in `_stage_standard_shud_forcing`
  (`manifest["forcing"]` + `self.config`) — passed into the anchor as
  arguments, with NO new parameters on `_prepare_shud_project_forcing` and
  NO `_ForcingPackageContext` change; when forcing_uri is unavailable,
  uri-derivation is skipped for that entry (dot-normalization still
  applies).
- The issue's 解决思路 suggests optionally landing the `./` half separately
  from the uri half; this change deliberately collapses both halves into
  one delivery (consistent with the issue's own S/M single-change sizing —
  the uri half needs no extra plumbing after all).
- Both declaration sources covered: the package-manifest lane
  (`forcing_context.package_manifest["files"]`) and the run-manifest
  diagnostic fallback (`_forcing_checksum_entries`).
- Anchor tests per the issue's five acceptance scenarios (dot-prefix,
  uri-only, both lanes, invalid-skip non-raising, both-members→None).

## Capabilities

- `fixed-station-forcing-production`: MODIFIED requirement "Forcing package
  station-index identity is basin-neutral and fails closed" — one appended
  body sentence (declaration-source matching accepts the DG-accepted shapes,
  skip-not-raise) + 4 appended scenarios. Byte-faithful otherwise.

## Impact

- `workers/shud_runtime/runtime.py` (anchor + threading), `tests/test_shud_runtime.py`.
- Out of scope (issue boundary): DG lane's own
  `_authoritative_package_manifest_checksum_entries`/normalization semantics;
  canonical-first fallback policy itself; producer output shape;
  `forcing_package.json` JSON Schema (separate issue if wanted); #1355 input
  workspace residue deletion.
- Rejected alternative (recorded): reusing
  `_authoritative_package_manifest_checksum_entries` under try/except —
  all-or-nothing (one bad entry kills the whole anchor) and
  exception-driven control flow for best-effort parsing.
