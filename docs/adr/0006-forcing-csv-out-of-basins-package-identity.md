# ADR 0006: Forcing CSV payloads are not part of basins package identity

- Status: Accepted
- Date: 2026-08-25
- Issue: #1813 (blocks #1702 item 3)
- Supersedes: nothing. Related: ADR 0005 (recalibration state carryover), #1080 (cutover gate fail-safe), #1720.

## Context

A basins package manifest declared `forcing.policy = "excluded_by_default"` and
`payload_copied = false`, and in the same document folded `csv_count`,
`byte_count`, and `aggregate_checksum` — each derived by sha256-reading every
forcing CSV — into `content_sha256` and `package_checksum`. The `copy_forcing`
flag gated only whether payloads were appended to the package contents; it never
gated the scan or the hash. Production always published with
`copy_forcing=False`, so the contradiction was the permanent production state.

A second, independent leg carried the same dependency: discovery wrote
`forcing_csv_count` into the inventory document, and the whole document was
hashed raw into `source_inventory_checksum`, which the cutover gate treats as a
nested model identity field.

Both legs feed `scheduler_file_provider_refresh`'s `package_changed`
classification, and any undeclared `package_changed` is `refused` by design
(#1080). The consequence: **deleting an unused forcing CSV and recalibrating a
basin's mesh were indistinguishable at the cutover gate.** Pure operations and
scientific model change demanded the same per-basin declared cutover.

The churn is not local. The chain was traced end-to-end for this decision:

```
forcing/*.csv -> aggregate_checksum -> checksum_material -> package_checksum
  -> _package_identity -> model_input_package_id -> _mint_model_id -> dg_<hash>
  -> hydro.hydro_run.model_id
```

So each declared cutover is equivalent to a new model downstream, requiring
#1698-style warm carry-over. Huai-MAIN's fragmentation into 96/2/3/4 runs across
three days of identity churn is the observed form of this.

The standing cost, paid every baseline publish round: ~126 GB of forcing tree
read across 18 basins, to produce an aggregate checksum whose only consumer was
the identity material it should not have been in.

## Decision

**Forcing CSV content is not part of basins package identity.** The declaration
becomes true rather than the code becoming honest about hashing.

- The forcing contribution to `package_checksum` and `content_sha256` is the
  declaration itself: `{"policy", "payload_copied"}` — for **both** policies.
  When payloads are copied they already enter identity as `included_files`
  entries with `role="forcing"`, exactly as calibration content does, so the
  copied case loses no coverage.
- When forcing is not copied, no CSV payload is read end-to-end. Count and bytes
  come from `stat`. Publication cost stops scaling with historical forcing volume.
- `forcing_dir_original_name` leaves the version-string source material.
- Discovery stops emitting `forcing_csv_count`. It had no production reader, and
  it was the only payload-derived field in the inventory document.
- `BASINS_PACKAGE_SCHEMA_VERSION` moves to `basins.package.v2` (and the
  discovery schema to `basins.discovery.v2`). The constant already sits inside
  `content_material`, so the bump *is* the migration: identity re-mints under a
  declared packaging schema change rather than a silent content change.
- Pre-migration material shapes are retained, not deleted, because published
  manifests are immutable evidence. Anything reconstructing a stored manifest's
  `package_checksum` selects the shape declared by that manifest's own
  `schema_version`.

### What this does and does not neutralize

| operation on a registered basin | identity |
|---|---|
| delete or edit forcing CSVs, keep the `forcing/` directory | **unchanged** |
| remove the `forcing/` directory outright | **changes** |
| rename legacy `focing/` to `forcing/` | **changes** |

`forcing_dir` and `forcing_dir_original_name` remain in the inventory because
packaging resolves the forcing source directory from them. They are structural
source facts, not payload evidence, and it is correct that they move identity.

**This constrains #1702 item 3.** Its procedure as originally written — `mv` the
whole `forcing/` directory to `Basins-retired/` — is a structural change and
still trips the cutover gate. The procedure must instead move the CSVs and leave
the (now empty) `forcing/` directory in place. This costs nothing and is what
makes #1702 item 3's Evidence Floor satisfiable as written.

## Alternatives rejected

**B — do not change code; batch the cleanup with the next recalibration and push
18 basins to a forcing-free version through #1697's `state_compatibility`
channel.** Fixes nothing: the contradiction persists, the per-publish I/O
persists, every future forcing touch re-poses the question, and it requires
scheduling 18 recalibrations that have no other reason to happen.

**C — keep forcing in identity but add a declarable "forcing-metadata-only
drift" category at the cutover gate.** The gate compares opaque hashes. To
classify drift by component, identity would have to be decomposed into
components stored in the registry row — strictly more identity surface and a
larger contract change than deleting the component that already declares itself
out of the package. It also touches the gate, which #1813 placed out of scope.

## Consequences

**Accepted cost: every basin's identity changes once, at its next baseline
publish.** This is deliberate and named — the schema-version bump is what makes
the change attributable to a declared packaging migration instead of looking
like silent content drift.

It is amortized, not big-bang: `refresh` re-emits the previous snapshot and
carries untouched basins' registry rows forward from the baseline registry file
rather than recomputing them. A basin's identity moves only when that basin is
actually re-packaged — an operator-initiated act that already needs a declared
cutover for its own recalibration. Ordering:

1. This change merges. No registry row moves; nothing is republished.
2. #1702's cleanup can start immediately; removing CSVs recomputes nothing.
3. Each basin's one-time churn lands at its next declared-cutover republish.

**Reversal is hard.** Restoring forcing to the identity material would re-mint
every identity a second time and invalidate reconstruction of v2 manifests.
Treat the pre-migration shape as permanent evidence, not dead code.

**Two implementations of the material must stay in step**:
`workers/model_registry/basins_package.py` owns it;
`services/production_closure/object_store_validation.py` reconstructs stored
manifests through the same seam. A parity test binds them across both schema
generations, and it writes the v1 shape as a literal so a refactor cannot
silently move historical reconstruction.

## Not decided here

The cutover gate's fail-safe `refused` semantics are unchanged and correct
(#1080). #1720's prospective≡previous defect on direct-grid topologies is
adjacent and untouched.
