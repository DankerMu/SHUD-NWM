# Design

## Risk triage

- **Fixture level**: `expanded`. Issue #1813 predates the pipeline's
  `Suggested fixture level` field, so triage is orchestrator-set. Justification:
  a packaging-identity contract change that is hard to revert (identities
  re-mint), feeds the cutover gate's inputs, and has a verified downstream
  amplification into `hydro_run.model_id`.
- **Risk packs selected**: identity-and-checksum contracts; migration /
  backward-compatibility of persisted artifacts; duplicate-implementation drift.
- **Risk packs not selected**: DB/PostGIS semantics (no schema or query change);
  Slurm parity (no scheduling change); frontend contract (no API surface
  change); numerical stability (no physics touched).
- **Must-preserve**: cutover gate fail-safe (`refused` for undeclared
  `package_changed`, #1080) unchanged; `BASINS_PACKAGE_CHECKSUM_CONFLICT` hard
  failure on same-version repackage unchanged; `copy_forcing=True` must lose no
  identity coverage; historical manifests must stay reconstructable.
- **Seams under test**: `_forcing_checksum_material` (both implementations),
  `basins_package_source_identity`, `_sha256_bytes(inventory_bytes)`,
  `_package_checksum_from_stored_manifest`.
- **Non-goals**: performing #1702's cleanup; changing the cutover gate;
  #1720; renaming legacy `focing/`.

## The verified downstream amplification (closes an acceptance checkbox)

Issue #1813 marked this inferred and asked the implementer to prove or disprove
it. It is **true**; the chain is closed end-to-end, read at HEAD:

```
forcing/*.csv content
  -> _forcing_metadata aggregate_checksum      basins_package.py:1155-1221
  -> _forcing_checksum_material                basins_package.py:1466-1475
  -> checksum_material["forcing"]              basins_package.py:183-196
  -> package_checksum                          basins_package.py:196
  -> _package_identity(...package_checksum...) provision_direct_grid_scheduler_registry.py:291-301
  -> model_input_package_id = f"dg-input-{identity}"   :365
  -> _mint_model_id(basin_version_id, canonical_grid_key,
                    model_input_package_id, binding_checksum)
                                     direct_grid_variant_registration.py:435-456
  -> dg_<32hex>
  -> hydro.hydro_run.model_id
```

Corroborated in production: Huai-MAIN fragmented into 96/2/3/4 runs across three
days of identity churn. Priority confirmed **P1**.

## Decision: option A, with the cleanup semantics pinned

Issue #1813 listed three unreviewed options and deferred the choice. The choice
is made here rather than left for review.

**Chosen — A: take forcing out of the identity material.**

The codebase already states the principle A enforces: `calibration` enters
identity through `included_files` as real package content
(`basins_package.py:1446-1452`), and identity means "what the package contains,
plus its declared policy". `forcing` is the single role that declares itself
excluded and hashes anyway. A removes a deviation; it does not introduce a new
rule.

**B (batch the cleanup with the next recalibration) — rejected.** It fixes
nothing: the contradiction persists, the 126 GB per-publish I/O persists, every
future forcing touch re-poses the question, and it requires scheduling 18
recalibrations that have no other reason to happen.

**C (a declarable "forcing-metadata-only drift" category at the gate) —
rejected.** The gate compares opaque hashes (`package_checksum`,
`source_inventory_checksum`). To classify drift by *component*, the identity
would have to be decomposed into components stored in the registry row — strictly
more identity surface and a larger contract change than deleting the component
that already declares itself out of the package. It also violates the issue's own
out-of-scope boundary on the cutover gate.

Systemic argument: every dg_* churn incident this repo has spent effort on
(#1826's Huai-MAIN fragmentation, #1816's 16 backfills) is triggered by
package-identity drift. A shrinks the trigger surface of that incident class.

### Acceptance-criterion fork, stated explicitly

#1813's criterion 4 offers "rewrite #1702's Evidence Floor" or "eliminate the
cutover trigger". **We choose eliminating the trigger.** #1702 item 3's Evidence
Floor holds as written.

## Material shape

`_forcing_checksum_material` returns `{"policy", "payload_copied"}` for **both**
policies, not only the excluded one. When `copy_forcing=True` the forcing files
are already `included_files` entries with `role="forcing"`, and their
`relative_path`/`size_bytes`/`sha256` are hashed into `actual_checksum_material`
(`basins_package.py:295-305`, recomputed at `:1478-1508`) — verified, so the
copied case loses no identity coverage. Keeping the five scan fields in the
material for the copied case would be redundant identity surface.

Manifest evidence (`csv_count`, `byte_count`, `time_coverage`, `sample_headers`,
`forcing_dir`, `forcing_dir_original_name`) stays in the manifest's `forcing`
block, untouched and operator-useful.

## The second leg: `source_inventory_checksum`

`source_inventory_checksum` is a raw-bytes hash of the whole inventory document,
derived at **two** sites that must agree byte-for-byte:

- write: `basins_package.py:320` `_sha256_bytes(inventory_bytes)`
- verify: `basins_registry_import.py:1927`
  `inventory_raw_checksum or _sha256_json(inventory)`, fed from
  `basins_registry_import.py:118,147,188` and
  `qhh_production_bootstrap.py:969`

Hashing a *projection* of the document was considered and rejected: it forks the
"hash of the file I read" provenance property across five call sites. Instead
the forcing-derived fields that the cleanup would flip are removed at the source.

Full enumeration of forcing-derived inventory fields
(`basins_discovery.py:275-300`):

| field | varies with | disposition |
|---|---|---|
| `forcing_dir` (`:282`) | directory presence + name | **kept** — real readers: `basins_package.py:1115`, `forcing_producer/file_store.py:511` |
| `forcing_dir_original_name` (`:283`) | directory presence + name | **kept** — real reader: `basins_package.py:1071-1080` resolves the source dir from it |
| `forcing_csv_count` (`:291`) | CSV count | **dropped** — grep across `*.py`/`*.json`/`*.ts`/`*.md`/`*.sql` finds only the writer, tests, and an archived tasks.md |
| `quirks[]` (`:286`) | `forcing_dir_conflict` when both `forcing/` and `focing/` exist (`:487`) | **kept** — genuine ambiguity evidence, not payload-derived |
| `checksums` (`:293`) | — | not forcing-derived; `_checksums_for_required_files` covers required input files only |

This is why the cleanup semantics must be pinned: with the directory kept and
only its CSVs removed, `forcing_dir`, `forcing_dir_original_name`, and `quirks`
are all unchanged, `forcing_csv_count` no longer exists, and the inventory bytes
are identical. Deleting the directory outright would still drift, correctly —
that is a structural change, not a payload cleanup.

## The duplicate implementation (blocking coupling)

`services/production_closure/object_store_validation.py:971-983` holds a
byte-identical private copy of `_forcing_checksum_material`, used at `:894` by
`_package_checksum_from_stored_manifest` to **reconstruct and verify
`package_checksum` for already-published manifests** (`:802-810`). Changing the
packager's material without mirroring here flips
`package_checksum_confirmed_from_stored_manifest` to false for every package.

Because reconstruction targets *historical* manifests, mirroring alone is not
enough: it must branch on the stored manifest's own `schema_version` — old shape
for pre-bump manifests, the constant for post-bump ones. This is what makes the
`BASINS_PACKAGE_SCHEMA_VERSION` bump load-bearing rather than cosmetic. The
existing reconstruction-limitation plumbing (`:849-851`) is the graceful-
degradation channel to reuse rather than reinvent.

A parity test binding the two implementations across both schema generations is
non-negotiable; whether they are additionally unified into one shared function is
the implementer's call by import-graph convention.

## Churn: amortized, not big-bang

The schema bump changes every basin's identity at its **next** baseline publish —
independently of forcing deletion. That is deliberate: the churn is named as a
declared packaging migration rather than looking like a silent content change.

It is not a big-bang because `refresh` re-emits the previous snapshot and carries
untouched basins' registry rows forward from the baseline registry file rather
than recomputing them. A basin's identity moves only when that basin is actually
re-packaged — an operator-initiated act that already needs a declared cutover for
its own recalibration.

Ordering that makes both claims true at once:

1. This change merges. No registry row moves; nothing is republished.
2. #1702's cleanup (empty the `forcing/` directories) can start **immediately**;
   deletion alone recomputes nothing.
3. Each basin's one-time identity churn lands at its next declared-cutover
   republish, attributable to the schema migration.

## Verification approach

The red test must exercise **re-discovery**, not a hand-edited inventory:
publish with forcing present -> empty `forcing/` the way #1702 will -> re-run
discovery -> re-publish -> assert `content_sha256`, `source_sha256`,
`package_checksum`, and `source_inventory_checksum` are all identical. Built this
way the test mechanically catches any forcing-derived inventory field the
enumeration above missed. A companion test mutates CSV bytes in place and
asserts the same four values are unchanged.
