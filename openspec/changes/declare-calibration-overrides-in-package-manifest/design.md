# Design

## Risk triage

**Fixture level**: standard, leaning high. This edits published model content —
the same surface #1816 removed for doing it silently. Getting it wrong twice in
the same place is the risk to design against.

**Risk surfaces selected**

| Surface | Why | Covered by |
|---|---|---|
| Silent data rewrite | The defect #1816 removed; must not return | D1, D3 |
| Provenance | The record has to travel with the package, not sit in a workspace | D2 |
| Identity churn | Any package change re-derives `dg_*` and orphans per-model artifacts | D4 |
| Source-tree mutation | The Basins tree is external users' data | D1 |

**Not selected**: concurrency (publication already holds a per-model lock),
auth (no new actor), schema migration (an added optional manifest field).

**Must-preserve**: publication stays a pure copy for everything not declared;
the Basins source tree is never written; already-issued forecast history is not
re-signed.

## D1 — Declaration first, isolated copy second, never the source tree

The override is data, not logic: a checked-in file that a human edits and review
sees in the diff. The publisher reads it and applies only what it names.

Three properties, each chosen against a specific way the old repair failed:

- **Named, not scanned.** The old repair walked every basin and clamped whatever
  exceeded a constant, so it silently cut seven basins that measurement now shows
  never needed it. An override applies to exactly one (basin, parameter) pair.
- **Declared value, not derived value.** The old repair computed the value
  (`19.999 / max(Alpha column)`), so no one could see what a package would get
  without re-deriving it. The declaration states the number.
- **Isolated copy.** Applied on a staging copy of the basin tree, reusing the
  existing `repaired-basins` staging pattern in
  `scripts/publish_scheduler_file_registry.py`. The Basins tree is other people's
  data and stays read-only.

## D2 — The record goes in the manifest, because that is what travels

#1816's most damning measurement: of eight rewritten packages, exactly one
publish receipt survived, in a scratch directory. The receipt lives in the
publisher workspace and is written only when `--output` is passed; the package
itself said nothing.

So the override record goes into `manifest["calibration"]`, which already exists
(`basins_package.py:_calibration_metadata`) and is already part of the manifest
the package carries. `publish_basins_package` gains an argument for it. The
receipt keeps its `summary["repairs"]` entry as well — two records, but only one
of them is authoritative and it is the one attached to the bytes.

Because the manifest feeds `package_checksum`, recording an override changes the
package identity. That is correct: a package with a different calibration IS a
different package.

## D3 — Refuse, never skip

A declaration that cannot be applied is an operator error, and the failure mode
that matters is the silent one: an entry that names a basin that was renamed, or
a parameter spelled wrong, would leave the package published with the ORIGINAL
value while the declaration file claims otherwise. Everyone downstream then
reads a lie that is worse than having no declaration at all.

Therefore: unknown basin, unknown parameter, unparseable value, or a declared
entry that matched nothing → the publish fails, naming the entry.

## D4 — Applying an override re-derives the model identity

`package_checksum` → `_package_identity()` → `dg_<32hex>`. Publishing hetianhe
with `GEOL_DMAC = 4` therefore mints a new `model_id`, which orphans that model's
forcing for every already-produced cycle and breaks its warm-state lineage —
the exact failure #1825 was built for.

This is not something to design away; it is the standing consequence of content
addressing (and the subject of #1813). It is called out here so the rollout is
planned rather than discovered: publish → backfill forcing under the new id
(`scripts/node22_backfill_forcing_for_model_ids.py`) → clone the warm state →
release the failed run (`scripts/node22_manual_retry_failed_runs.py`) → one
bounded pass.

## D5 — Why `GEOL_DMAC = 4` and not 4.5

Measurement puts the NaN cliff between 4.5 and 4.75, on two independent samples
(gfs and IFS, different forcing and different warm state). 4.5 is the largest
verified value and is ~5% below the cliff; 4.0 has months of production history
and is ~11% below it.

The deciding argument is that neither is the modeller's value, so "closer to
source" buys nothing scientific, while the margin is the only thing protecting
against a future cycle whose forcing shifts the cliff. Take the margin. The
declaration records the measured cliff so the next person does not have to
re-derive it.

## Seams under test

- Declaration parsing and its four refusals.
- Override application on the staging copy, with the source tree asserted
  unchanged.
- Manifest recording, including that a non-overridden basin gets no field.
- Pure-copy preservation: a basin absent from the declaration publishes
  byte-identical calibration files.

## Evidence mapping

| Requirement | Evidence |
|---|---|
| Only declared pairs are overridden | unit test over a multi-basin fixture |
| Source tree never written | unit test asserting source bytes after publish |
| Override travels in the manifest | unit test on manifest content |
| Undeclarable entries refuse | unit tests, one per refusal |
| hetianhe actually runs | node-22 pass receipt after the rollout in D4 |
