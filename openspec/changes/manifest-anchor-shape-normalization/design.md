# Design: manifest-anchor-shape-normalization

## Change surface

`workers/shud_runtime/runtime.py`, current-head coordinates (issue cites
63206af7 of the #1176 branch — re-locate by symbol): anchor
`_manifest_declared_shud_forcing_index_member` :3960-3985; run-manifest lane
`_forcing_checksum_entries` :3988; DG normalizer
`_normalize_package_manifest_file_relative_path` :4055-4074 and deriver
`_derive_package_manifest_file_relative_path` :4077-4097 (both raising —
reused, not modified); feed site `_prepare_shud_project_forcing` :961-1032
(anchor-feed block :984-993); anchor call site `_stage_standard_shud_forcing`
:1034 with consumer :1074-1079 (None → canonical-first);
`_ForcingPackageContext` :180-183 (shared with DG path — do NOT extend).

Risk triage: compact fixture. Matching-widening on a best-effort resolver,
with a behavioral delta in BOTH directions: previously-unmatched shapes now
match, and a mixed-shape declaration of *both* members that today resolves
by accident (the non-plain declaration being invisible to the anchor) now
correctly returns None and falls back canonical-first — this narrowing is
the intended semantics, pinned by the spec delta's both-members scenario.
Highest risks: (1) accidentally adding a raise path to the non-DG lane
(forbidden), (2) changing DG-lane behavior (out of scope — the anchor is
only consulted on the non-DG branch after the DG fail-closed raise at
:1062-1073), (3) the both-members ambiguity semantics must survive shape
mixing.

## Key decisions

1. **Non-raising wrapper, per-entry skip**: wrap the existing raising
   normalizer in `try/except SHUDRuntimeError: continue` per entry (or a
   `_or_none` variant). Rationale over the rejected try/except-whole-list
   alternative: one malformed entry must not blind the anchor to a valid
   sibling declaration. The wrapper reuses
   `_normalize_package_manifest_file_relative_path` VERBATIM as the single
   source of shape semantics — no re-implementation, so DG and anchor cannot
   drift again (the root cause of #1357 was "reused the data contract but
   not the parser").
2. **No new threading — both values are already in scope at the anchor call
   site**: `_stage_standard_shud_forcing` (:1034) is a `SHUDRuntime` method
   and already receives `manifest`, so at the :1074 call the implementer
   reads `manifest["forcing"].get("forcing_uri")` and
   `self.config.object_store_prefix` directly — the same two sources the DG
   lane uses at :1173-1174. No new parameter on
   `_prepare_shud_project_forcing`, no `_ForcingPackageContext` field (that
   dataclass is shared with the DG path — extending it widens blast radius
   for nothing). `_stage_standard_shud_forcing` has exactly one caller
   (:994), so passing the values into the anchor as arguments is a clean
   seam. When forcing_uri is unavailable/empty, the wrapper skips
   uri-derivation entries (dot-normalization still applies — it needs no
   uri context).
3. **Ambiguity semantics unchanged**: normalization runs BEFORE the
   accepted-member intersection; `len(matched) == 1` logic untouched. Mixed
   shapes declaring both members → None → canonical-first (pinned).
4. **DG lane untouched**: `_authoritative_package_manifest_checksum_entries`
   and the raising normalizer/deriver bodies unchanged; anchor docstring
   updated to record the shape tolerance and its reuse of the DG parser.

## Must preserve

- Zero new raise paths reachable from the non-DG staging lane (the :981-983
  comment's rationale stands: `_authoritative_package_manifest_checksum_entries`
  is deliberately not used because it would newly fail-close this lane).
- DG behavior byte-identical (raising normalizer semantics untouched).
- Anchor `None` on zero/both matches; canonical-first fallback consumer
  :1074-1079 untouched.
- Plain-shape matching identical (existing anchor tests stay green).
- Producer output untouched.

## Seams under test

- Direct anchor-function tests (fast, shape matrix) plus staging-level tests
  through `_stage_standard_shud_forcing`/`_prepare_shud_project_forcing`
  with dual members on disk (the issue's acceptance is anchored at staging
  outcome: "解析出 legacy 成员，不退回 canonical"). NOTE: no direct
  anchor-function tests exist today — the existing regression set is
  staging-level in tests/test_shud_runtime.py at :1661, :1691, :1728,
  :1772, :1808, :1843, with helpers `_write_residual_index_member` (:1550),
  `_rewrite_package_manifest` (:1577), `_assert_staged_index_member_content`
  (:1646); the DG dot-prefix pin is
  `test_runtime_direct_grid_checksum_cap_uses_normalized_staged_tsd_path`
  (:2427, `./`-prefix injection :2452). These named tests are the "existing
  tests stay green" set for task 2.6.

## Test plan (maps to acceptance)

1. Package-manifest lane, legacy declared `./shud/qhh.tsd.forc`, dual
   members staged → legacy staged (not canonical).
2. Package-manifest lane, legacy declared uri-only (uri under forcing_uri,
   no relative_path), dual members → legacy staged.
3. Run-manifest fallback lane: same two shapes.
4. Invalid/underivable entry (uri not under forcing_uri) → entry skipped,
   anchor falls back canonical-first, NO raise (assert staging completes).
5. Both accepted members declared, mixed shapes → None → canonical-first
   (existing semantics pinned).
6. Existing plain-shape anchor/staging tests untouched and green; DG tests
   untouched and green.

## Risks to watch

- The run-manifest lane's entries always carry `uri` (raising lane enforces
  it), but the ANCHOR must not inherit that enforcement — entries reach the
  anchor via the non-raising path only.
- Non-DG `forcing_uri` may be a tar (:1886-1891 rejects tars only for DG;
  non-DG stages them via `_stage_tar_artifact_bytes` :1894; see the
  `…/demo_model.tar` shape at tests/test_shud_runtime.py:2334). With a tar
  uri, `_derive_package_manifest_file_relative_path` raises for every entry
  → all uri-only declarations are skipped. This is SAFE (degrades to
  today's behavior, no misresolution) and expected — do not "fix" it, and
  do not mistake the no-op for a bug later.
- Duplicate same-member declarations (`shud/x.tsd.forc` + `./shud/x.tsd.forc`)
  collapse in the set to one element → resolve to that member, both before
  and after the fix (probe-confirmed zero delta). Neither "matched twice"
  nor "ambiguous"; covered as an extra case in task 2.5.
