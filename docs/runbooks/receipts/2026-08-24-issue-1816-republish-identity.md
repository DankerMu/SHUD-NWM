# #1816 republish model-id replacement and first-admitted-run evidence (8 basins, 16 ids)

- Republish: #1816 hop 3 (the repair that silently rewrote calibrated parameters was removed, PR #1817). The
  republish receipt is the #1816 issue comment
  <https://github.com/DankerMu/SHUD-NWM/issues/1816#issuecomment-5391626596> (2026-08-24T06:38Z). This file is its
  living-repo copy of the identity map plus the first-pass transition evidence that comment deferred (#2365).
- Capture: 2026-09-26; `captured-at.txt` records `2026-09-26T08:28:19Z`. Read only. Nothing ran on node-22.
  - Manifests and the state index were read on node-27 through the NFS mount
    (`/home/ghdc/nwm/object-store/scheduler/`, the same share as node-22 `/ghdc/data/nwm/object-store/`).
  - `hydro.hydro_run` was read on node-27 (`/home/nwm/NWM`) with the display role from `infra/env/display.env`,
    inside `BEGIN READ ONLY; ... COMMIT;`.
- Supersedes the M1′ record of runbook §5.7.1
  ([`recalibration-and-archive.md`](../production-ops/recalibration-and-archive.md)): the Huai-MAIN M1′ ids
  `dg_281ff8c7ccd761239bd39dc44977138f` (gfs) and `dg_03b3cd97dec0e4847ed207347bc251a4` (ifs) were replaced by
  `dg_5bd9935f3c32ac5b2936f68588489575` (gfs) and `dg_67210bfe424cdcdc69aaa7469485a382` (ifs).

## 1. Old -> new map (16 rows)

Sources: `scheduler/registry/manifest-last.json.pre-republish-1816-20260824T062347Z` (`generated_at`
2026-08-24T02:23:33Z, old) against `scheduler/registry/manifest-last.20260824T225718Z.bak.json` (`generated_at`
2026-08-24T06:24:35Z, new). Both manifests hold 48 `(basin, source)` keys, the key sets are equal, and exactly 16
rows changed `model_id`. All 16 new rows were `active_flag=True`, `lifecycle_state=active` in the new manifest.
Checksums are the first 16 hex characters of `package_checksum`; the source column is parsed from the variant
directory in `manifest_uri` / `model_package_uri`.

| basin | source | old `model_id` | old variant dir | new `model_id` | new variant dir | old pkg checksum (16) | new pkg checksum (16) |
|---|---|---|---|---|---|---|---|
| `basins_heihe` | gfs | `dg_d3da0d68ab5bb27b525f8d9e030b898d` | `dg-gfs-ad6ebbae7d2aea5a541b` | `dg_f6175cdb0f3825bec4807c386b5cbf38` | `dg-gfs-30fffff33ad6d8dd4591` | `a1bf961fd3e718c0` | `b2dbe6329224f316` |
| `basins_heihe` | ifs | `dg_af93a48636397f87722bdbd11ceb99a3` | `dg-ifs-9911aae73b2fc56da144` | `dg_8543132517bba75279944a5960badcb7` | `dg-ifs-dfde4c1b7744d4a00b7b` | `266f4668b83af734` | `332d0553ed9f92d4` |
| `basins_hetianhe` | gfs | `dg_688c7bb90fd97da9befaee33cc428ff3` | `dg-gfs-319443d3bf755b52d59e` | `dg_292e1fd6e2d54fc3c800f6df07116d0e` | `dg-gfs-26d1c375142eaa25d758` | `3124fd2b51afd863` | `d94cd2b40328bc08` |
| `basins_hetianhe` | ifs | `dg_e404644e4705102949d42b5331ee6086` | `dg-ifs-c3db715a3d7b06ba474c` | `dg_07c146effd85828ee536d0033e2591b3` | `dg-ifs-499e45f2a175371544a5` | `ecc8d478bbcd61a8` | `a4f57bd9487a1933` |
| `basins_huai_main` | gfs | `dg_281ff8c7ccd761239bd39dc44977138f` | `dg-gfs-8ea4a282d425727e7ea1` | `dg_5bd9935f3c32ac5b2936f68588489575` | `dg-gfs-04f29820d9c487ab41ba` | `2f5e3c2f475ee484` | `ed796f5435dc0a61` |
| `basins_huai_main` | ifs | `dg_03b3cd97dec0e4847ed207347bc251a4` | `dg-ifs-9ddadb8ee989ec84730c` | `dg_67210bfe424cdcdc69aaa7469485a382` | `dg-ifs-d1b1b2c6d13a8e719381` | `fdc30af6bb3193a1` | `8ac9c483cd6f84d3` |
| `basins_qhh` | gfs | `dg_4a3c03155c9e467ba667fbd69efb18be` | `dg-gfs-00c7b9ac62e74c7e7565` | `dg_0883c7e9c1006c6fd347df500315e9df` | `dg-gfs-f7751638e3b5ef6811b6` | `f14611a718cce14b` | `53fd4e6c3e0c32ab` |
| `basins_qhh` | ifs | `dg_9d318f475991068cb3d72d0f3dea4ba5` | `dg-ifs-dff402f29447e06b46e5` | `dg_9ccb261a39d51c24f4de9173fb4461b6` | `dg-ifs-1c63f7aba4ba22acce7a` | `bac0edd5139bf411` | `3b9ebfe8fa1a74ad` |
| `basins_qinyijiang` | gfs | `dg_98d20f5c16f05bbe7e1f9f72234742fd` | `dg-gfs-f774194a3d4303e34ba3` | `dg_f8d0ceab6c99000880b772ce138f7ed8` | `dg-gfs-5161d96e1ce18a1a2175` | `ced209ebb8b00320` | `4ff145c2dc694cac` |
| `basins_qinyijiang` | ifs | `dg_cdab38c99bf9fd1fdb6fa9e95466b5ac` | `dg-ifs-3a7aa790d70f0df04419` | `dg_227f20efc981e3299b3e5bb2dfbf97a4` | `dg-ifs-96f671a3ba3a733f799e` | `f89d7b05ee52b5dd` | `7f84cb58646472c1` |
| `basins_shj_2shj` | gfs | `dg_2a26a183d131ce80987dbff37d994839` | `dg-gfs-0e723287d336957b37df` | `dg_95b0a3efb58fc525a1401d0c8f2b416f` | `dg-gfs-5ee078bb12f4f6ce0ed9` | `717f37d7988d06b2` | `8181952e97ddd27b` |
| `basins_shj_2shj` | ifs | `dg_5b6d9f6a10a34f2e703a36f0312dccb9` | `dg-ifs-c3590910c2010c7b21e9` | `dg_4d2ddab541048593d247d42da317fa3c` | `dg-ifs-1776186a637f43e826c3` | `55f3c8f2fcd69815` | `ce24dd296cacf6c0` |
| `basins_tailanhe` | gfs | `dg_f14b54044d10944d60d66a0acca4d409` | `dg-gfs-428b06117bd0b5bdeae2` | `dg_510fbd39433e9cd2ee948980efd523e1` | `dg-gfs-b3501f2c4219b59a839d` | `5d7a66b6f886c784` | `9f3a3468179d2e75` |
| `basins_tailanhe` | ifs | `dg_04405f8ced65fec0aae4d56d3d31a850` | `dg-ifs-21aede815c97e3054fe7` | `dg_7e247591d8fae09dc8db71482c73efee` | `dg-ifs-ecad8fc4bc33fce3910e` | `04389149735ca02b` | `34bbd541ceb0fe64` |
| `basins_weiganhe` | gfs | `dg_e5344485e0e71079165927cda2046233` | `dg-gfs-a4360008e04a765eb0c4` | `dg_63f9fbca402ecb56e951d3c89a5100bf` | `dg-gfs-4bb86bd6d8d257924936` | `59499db657da4135` | `0deb3dc73805b6e1` |
| `basins_weiganhe` | ifs | `dg_a9ec352ed3a850645dd9fe965bc8dd7e` | `dg-ifs-182eaf9d45541ab3f8f4` | `dg_ba26785ea1e776f842e6916eb673a9ed` | `dg-ifs-0f549ad66db928a73b90` | `7c98306ceac8690f` | `64fc234ed673f9e1` |

Check script `p-manifest.py` (stdlib only, run on node-27 against the NFS path):

```python
import json
D = "/home/ghdc/nwm/object-store/scheduler/registry/"
old = json.load(open(D + "manifest-last.json.pre-republish-1816-20260824T062347Z"))["models"]
new = json.load(open(D + "manifest-last.20260824T225718Z.bak.json"))["models"]
import re
def src(m):
    g = re.search(r"dg-(gfs|ifs)-[0-9a-f]+", m.get("manifest_uri","") + " " + m.get("model_package_uri",""))
    return g.group(0) if g else "?"
def key(m): return (m["basin_id"], src(m).split('-')[1] if src(m)!='?' else '?')
o = {key(m): m for m in old}; n = {key(m): m for m in new}
print("old", len(o), "new", len(n), "keysets equal", set(o) == set(n))
rows = []
for k in sorted(set(o) | set(n)):
    a, b = o.get(k), n.get(k)
    if a is None or b is None or a["model_id"] != b["model_id"]:
        rows.append((k[0], k[1], a and a["model_id"], a and src(a), b and b["model_id"], b and src(b), a and a["package_checksum"][:16], b and b["package_checksum"][:16], b and b["active_flag"], b and b["lifecycle_state"]))
print("changed", len(rows))
for r in rows: print("\t".join(str(x) for x in r))
print("uri sample", new[0].get("manifest_uri"))
```

Output (`1816-mapping.txt`, verbatim; the last line is the sample `manifest_uri` that shows the variant-dir shape
the regex parses). It is tab-separated and kept byte-for-byte, so the hard-tab lint rule is switched off around it:

<!-- markdownlint-disable MD010 -->
```text
old 48 new 48 keysets equal True
changed 16
basins_heihe	gfs	dg_d3da0d68ab5bb27b525f8d9e030b898d	dg-gfs-ad6ebbae7d2aea5a541b	dg_f6175cdb0f3825bec4807c386b5cbf38	dg-gfs-30fffff33ad6d8dd4591	a1bf961fd3e718c0	b2dbe6329224f316	True	active
basins_heihe	ifs	dg_af93a48636397f87722bdbd11ceb99a3	dg-ifs-9911aae73b2fc56da144	dg_8543132517bba75279944a5960badcb7	dg-ifs-dfde4c1b7744d4a00b7b	266f4668b83af734	332d0553ed9f92d4	True	active
basins_hetianhe	gfs	dg_688c7bb90fd97da9befaee33cc428ff3	dg-gfs-319443d3bf755b52d59e	dg_292e1fd6e2d54fc3c800f6df07116d0e	dg-gfs-26d1c375142eaa25d758	3124fd2b51afd863	d94cd2b40328bc08	True	active
basins_hetianhe	ifs	dg_e404644e4705102949d42b5331ee6086	dg-ifs-c3db715a3d7b06ba474c	dg_07c146effd85828ee536d0033e2591b3	dg-ifs-499e45f2a175371544a5	ecc8d478bbcd61a8	a4f57bd9487a1933	True	active
basins_huai_main	gfs	dg_281ff8c7ccd761239bd39dc44977138f	dg-gfs-8ea4a282d425727e7ea1	dg_5bd9935f3c32ac5b2936f68588489575	dg-gfs-04f29820d9c487ab41ba	2f5e3c2f475ee484	ed796f5435dc0a61	True	active
basins_huai_main	ifs	dg_03b3cd97dec0e4847ed207347bc251a4	dg-ifs-9ddadb8ee989ec84730c	dg_67210bfe424cdcdc69aaa7469485a382	dg-ifs-d1b1b2c6d13a8e719381	fdc30af6bb3193a1	8ac9c483cd6f84d3	True	active
basins_qhh	gfs	dg_4a3c03155c9e467ba667fbd69efb18be	dg-gfs-00c7b9ac62e74c7e7565	dg_0883c7e9c1006c6fd347df500315e9df	dg-gfs-f7751638e3b5ef6811b6	f14611a718cce14b	53fd4e6c3e0c32ab	True	active
basins_qhh	ifs	dg_9d318f475991068cb3d72d0f3dea4ba5	dg-ifs-dff402f29447e06b46e5	dg_9ccb261a39d51c24f4de9173fb4461b6	dg-ifs-1c63f7aba4ba22acce7a	bac0edd5139bf411	3b9ebfe8fa1a74ad	True	active
basins_qinyijiang	gfs	dg_98d20f5c16f05bbe7e1f9f72234742fd	dg-gfs-f774194a3d4303e34ba3	dg_f8d0ceab6c99000880b772ce138f7ed8	dg-gfs-5161d96e1ce18a1a2175	ced209ebb8b00320	4ff145c2dc694cac	True	active
basins_qinyijiang	ifs	dg_cdab38c99bf9fd1fdb6fa9e95466b5ac	dg-ifs-3a7aa790d70f0df04419	dg_227f20efc981e3299b3e5bb2dfbf97a4	dg-ifs-96f671a3ba3a733f799e	f89d7b05ee52b5dd	7f84cb58646472c1	True	active
basins_shj_2shj	gfs	dg_2a26a183d131ce80987dbff37d994839	dg-gfs-0e723287d336957b37df	dg_95b0a3efb58fc525a1401d0c8f2b416f	dg-gfs-5ee078bb12f4f6ce0ed9	717f37d7988d06b2	8181952e97ddd27b	True	active
basins_shj_2shj	ifs	dg_5b6d9f6a10a34f2e703a36f0312dccb9	dg-ifs-c3590910c2010c7b21e9	dg_4d2ddab541048593d247d42da317fa3c	dg-ifs-1776186a637f43e826c3	55f3c8f2fcd69815	ce24dd296cacf6c0	True	active
basins_tailanhe	gfs	dg_f14b54044d10944d60d66a0acca4d409	dg-gfs-428b06117bd0b5bdeae2	dg_510fbd39433e9cd2ee948980efd523e1	dg-gfs-b3501f2c4219b59a839d	5d7a66b6f886c784	9f3a3468179d2e75	True	active
basins_tailanhe	ifs	dg_04405f8ced65fec0aae4d56d3d31a850	dg-ifs-21aede815c97e3054fe7	dg_7e247591d8fae09dc8db71482c73efee	dg-ifs-ecad8fc4bc33fce3910e	04389149735ca02b	34bbd541ceb0fe64	True	active
basins_weiganhe	gfs	dg_e5344485e0e71079165927cda2046233	dg-gfs-a4360008e04a765eb0c4	dg_63f9fbca402ecb56e951d3c89a5100bf	dg-gfs-4bb86bd6d8d257924936	59499db657da4135	0deb3dc73805b6e1	True	active
basins_weiganhe	ifs	dg_a9ec352ed3a850645dd9fe965bc8dd7e	dg-ifs-182eaf9d45541ab3f8f4	dg_ba26785ea1e776f842e6916eb673a9ed	dg-ifs-0f549ad66db928a73b90	7c98306ceac8690f	64fc234ed673f9e1	True	active
uri sample s3://nhms/models/direct_grid_variants/basins_xinanjiang_upstream_shud/dg-gfs-fae3bd6daf6d137baea5/package/manifest.json
```
<!-- markdownlint-enable MD010 -->

## 2. First admitted run per new id

The scheduler does not persist the `warm_continue` label in the DB or the state index, so this section records an
inference and its basis, not a stored label. A pass that blocks leaves no `hydro_run` row, so the first
`forecast` row of a new id is its first **admitted** run.

Basis (code, cited by symbol; line numbers are helpers at the time of writing):

- `packages/common/state_clone.py` `_build_clone_row` (currently `:693`), called by `fingerprint_gated_state_clone`,
  writes the **target** identity into the clone row: `model_id=m1_model_id`,
  `model_package_checksum=m1_model_package_checksum`, `model_package_version=m1_model_package_version`, and records
  the origin in `cloned_from_model_id=source_snapshot.model_id`. The row therefore belongs to the new id's
  package generation.
- `services/orchestrator/scheduler_generation.py`: `derive_generation(package_checksum)` keys the generation on the
  package checksum, and `evaluate_transition_decision` returns `TransitionDecision.WARM_CONTINUE` in its branch
  "(e) Current-generation history exists" (currently `:1276`) when the current generation has the exact
  predecessor. A clone row at the candidate cycle is that same-generation predecessor.
- So when a new id's first admitted run starts from its own `state_compatibility` clone row, the decision on that
  pass was `warm_continue`, not a cold start (a packaged-IC bootstrap would leave `init_state_id` empty).

Limit: the captured clone-row fields (below) do not include the row's `model_package_checksum`. That it equals
the new package checksum is the `_build_clone_row` guarantee, not a captured value.

Clone provenance comes **only** from `scheduler/state-index/index-last.json`. The `LEFT JOIN hydro.state_snapshot`
in the query below returned NULL for every row (the four empty columns `init_state_model`, `init_state_run`,
`cloned_from_model_id`, `clone_gate_kind` of `1816-first-runs.txt`). That is consistent with the dated
[#1739 census](2026-09-15-issue-1739-clone-provenance-count-node27.md), which found `hydro.state_snapshot` empty on
2026-09-15.

Timing: the first admitted runs are cycles 2026-08-23 00Z (GFS) and 2026-08-23 12Z (IFS). Both cycle times are
before the republish; the new ids are absent from the pre-republish manifest (`generated_at` 2026-08-24T02:23:33Z)
and first appear in the one generated 2026-08-24T06:24:35Z, so these are backfill cycles. The wall-clock execution
time of those runs (`created_at`) was not captured.

| basin / source | new `model_id` | first forecast run (`cycle_time`, status) | `init_state_id` = clone-row `state_id` | clone row `cloned_from_model_id` (= old id) | `clone_gate_kind` | reading |
|---|---|---|---|---|---|---|
| `basins_heihe` / gfs | `dg_f6175cdb0f3825bec4807c386b5cbf38` | 2026-08-23 00:00:00+00, `published` | `state_gfs_dg_f6175cdb0f3825bec4807c386b5cbf38_2026082300_gfs_2026082212_f012` | `dg_d3da0d68ab5bb27b525f8d9e030b898d` | `state_compatibility` | `warm_continue` (inferred) |
| `basins_heihe` / ifs | `dg_8543132517bba75279944a5960badcb7` | 2026-08-23 12:00:00+00, `published` | `state_IFS_dg_8543132517bba75279944a5960badcb7_2026082312_ifs_2026082300_f012` | `dg_af93a48636397f87722bdbd11ceb99a3` | `state_compatibility` | `warm_continue` (inferred) |
| `basins_hetianhe` / gfs | `dg_292e1fd6e2d54fc3c800f6df07116d0e` | none (no `forecast` `hydro_run` row) | n/a (clone row `state_gfs_dg_292e1fd6e2d54fc3c800f6df07116d0e_2026082300_gfs_2026082212_f012` exists) | `dg_688c7bb90fd97da9befaee33cc428ff3` | `state_compatibility` | no first run; see Exceptions |
| `basins_hetianhe` / ifs | `dg_07c146effd85828ee536d0033e2591b3` | none (no `forecast` `hydro_run` row) | n/a (clone row `state_IFS_dg_07c146effd85828ee536d0033e2591b3_2026082312_ifs_2026082300_f012` exists) | `dg_e404644e4705102949d42b5331ee6086` | `state_compatibility` | no first run; see Exceptions |
| `basins_huai_main` / gfs | `dg_5bd9935f3c32ac5b2936f68588489575` | 2026-08-23 00:00:00+00, `published` | `state_gfs_dg_5bd9935f3c32ac5b2936f68588489575_2026082300_gfs_2026082212_f012` | `dg_281ff8c7ccd761239bd39dc44977138f` | `state_compatibility` | `warm_continue` (inferred) |
| `basins_huai_main` / ifs | `dg_67210bfe424cdcdc69aaa7469485a382` | 2026-08-23 12:00:00+00, `published` | `state_IFS_dg_67210bfe424cdcdc69aaa7469485a382_2026082312_ifs_2026082300_f012` | `dg_03b3cd97dec0e4847ed207347bc251a4` | `state_compatibility` | `warm_continue` (inferred) |
| `basins_qhh` / gfs | `dg_0883c7e9c1006c6fd347df500315e9df` | 2026-08-23 00:00:00+00, `published` | `state_gfs_dg_0883c7e9c1006c6fd347df500315e9df_2026082300_gfs_2026082212_f012` | `dg_4a3c03155c9e467ba667fbd69efb18be` | `state_compatibility` | `warm_continue` (inferred) |
| `basins_qhh` / ifs | `dg_9ccb261a39d51c24f4de9173fb4461b6` | 2026-08-23 12:00:00+00, `published` | `state_IFS_dg_9ccb261a39d51c24f4de9173fb4461b6_2026082312_ifs_2026082300_f012` | `dg_9d318f475991068cb3d72d0f3dea4ba5` | `state_compatibility` | `warm_continue` (inferred) |
| `basins_qinyijiang` / gfs | `dg_f8d0ceab6c99000880b772ce138f7ed8` | 2026-08-23 00:00:00+00, `published` | `state_gfs_dg_f8d0ceab6c99000880b772ce138f7ed8_2026082300_gfs_2026082212_f012` | `dg_98d20f5c16f05bbe7e1f9f72234742fd` | `state_compatibility` | `warm_continue` (inferred) |
| `basins_qinyijiang` / ifs | `dg_227f20efc981e3299b3e5bb2dfbf97a4` | 2026-08-23 12:00:00+00, `published` | `state_IFS_dg_227f20efc981e3299b3e5bb2dfbf97a4_2026082312_ifs_2026082300_f012` | `dg_cdab38c99bf9fd1fdb6fa9e95466b5ac` | `state_compatibility` | `warm_continue` (inferred) |
| `basins_shj_2shj` / gfs | `dg_95b0a3efb58fc525a1401d0c8f2b416f` | 2026-08-23 00:00:00+00, `superseded` | `state_gfs_dg_95b0a3efb58fc525a1401d0c8f2b416f_2026082300_gfs_2026082212_f012` | `dg_2a26a183d131ce80987dbff37d994839` | `state_compatibility` | `warm_continue` (inferred) |
| `basins_shj_2shj` / ifs | `dg_4d2ddab541048593d247d42da317fa3c` | 2026-08-23 12:00:00+00, `superseded` | `state_IFS_dg_4d2ddab541048593d247d42da317fa3c_2026082312_ifs_2026082300_f012` | `dg_5b6d9f6a10a34f2e703a36f0312dccb9` | `state_compatibility` | `warm_continue` (inferred) |
| `basins_tailanhe` / gfs | `dg_510fbd39433e9cd2ee948980efd523e1` | 2026-08-23 00:00:00+00, `published` | `state_gfs_dg_510fbd39433e9cd2ee948980efd523e1_2026082300_gfs_2026082212_f012` | `dg_f14b54044d10944d60d66a0acca4d409` | `state_compatibility` | `warm_continue` (inferred) |
| `basins_tailanhe` / ifs | `dg_7e247591d8fae09dc8db71482c73efee` | 2026-08-23 12:00:00+00, `published` | `state_IFS_dg_7e247591d8fae09dc8db71482c73efee_2026082312_ifs_2026082300_f012` | `dg_04405f8ced65fec0aae4d56d3d31a850` | `state_compatibility` | `warm_continue` (inferred) |
| `basins_weiganhe` / gfs | `dg_63f9fbca402ecb56e951d3c89a5100bf` | 2026-08-23 00:00:00+00, `published` | `state_gfs_dg_63f9fbca402ecb56e951d3c89a5100bf_2026082300_gfs_2026082212_f012` | `dg_e5344485e0e71079165927cda2046233` | `state_compatibility` | `warm_continue` (inferred) |
| `basins_weiganhe` / ifs | `dg_ba26785ea1e776f842e6916eb673a9ed` | 2026-08-23 12:00:00+00, `published` | `state_IFS_dg_ba26785ea1e776f842e6916eb673a9ed_2026082312_ifs_2026082300_f012` | `dg_a9ec352ed3a850645dd9fe965bc8dd7e` | `state_compatibility` | `warm_continue` (inferred) |

`runs` per new id and the old id's first/last `cycle_time` are in the raw output below (`runs_new`, `old_first`,
`old_last`). For Huai-MAIN the M1′ ids' `hydro_run` `cycle_time` range is 2026-08-22 00Z -> 2026-08-22 12Z (gfs)
and 2026-08-22 00Z -> 2026-08-23 00Z (ifs).

Query `p-hr2.sh` (node-27, display role, read only):

```bash
set -euo pipefail
cd /home/nwm/NWM
( set -a; . infra/env/display.env; set +a
  psql "$DATABASE_URL" -X -q -F '|' -A -v ON_ERROR_STOP=1 <<'SQL'
BEGIN READ ONLY;
WITH m(new_id, old_id) AS (VALUES
 ('dg_f6175cdb0f3825bec4807c386b5cbf38','dg_d3da0d68ab5bb27b525f8d9e030b898d'),
 ('dg_8543132517bba75279944a5960badcb7','dg_af93a48636397f87722bdbd11ceb99a3'),
 ('dg_292e1fd6e2d54fc3c800f6df07116d0e','dg_688c7bb90fd97da9befaee33cc428ff3'),
 ('dg_07c146effd85828ee536d0033e2591b3','dg_e404644e4705102949d42b5331ee6086'),
 ('dg_5bd9935f3c32ac5b2936f68588489575','dg_281ff8c7ccd761239bd39dc44977138f'),
 ('dg_67210bfe424cdcdc69aaa7469485a382','dg_03b3cd97dec0e4847ed207347bc251a4'),
 ('dg_0883c7e9c1006c6fd347df500315e9df','dg_4a3c03155c9e467ba667fbd69efb18be'),
 ('dg_9ccb261a39d51c24f4de9173fb4461b6','dg_9d318f475991068cb3d72d0f3dea4ba5'),
 ('dg_f8d0ceab6c99000880b772ce138f7ed8','dg_98d20f5c16f05bbe7e1f9f72234742fd'),
 ('dg_227f20efc981e3299b3e5bb2dfbf97a4','dg_cdab38c99bf9fd1fdb6fa9e95466b5ac'),
 ('dg_95b0a3efb58fc525a1401d0c8f2b416f','dg_2a26a183d131ce80987dbff37d994839'),
 ('dg_4d2ddab541048593d247d42da317fa3c','dg_5b6d9f6a10a34f2e703a36f0312dccb9'),
 ('dg_510fbd39433e9cd2ee948980efd523e1','dg_f14b54044d10944d60d66a0acca4d409'),
 ('dg_7e247591d8fae09dc8db71482c73efee','dg_04405f8ced65fec0aae4d56d3d31a850'),
 ('dg_63f9fbca402ecb56e951d3c89a5100bf','dg_e5344485e0e71079165927cda2046233'),
 ('dg_ba26785ea1e776f842e6916eb673a9ed','dg_a9ec352ed3a850645dd9fe965bc8dd7e'))
, first AS (
 SELECT DISTINCT ON (m.new_id) m.new_id, m.old_id, r.run_id, r.cycle_time, r.status, r.init_state_id
 FROM m JOIN hydro.hydro_run r ON r.model_id = m.new_id AND r.run_type::text='forecast'
 ORDER BY m.new_id, r.cycle_time, r.created_at)
SELECT f.new_id, f.cycle_time, f.status, f.run_id, f.init_state_id, s.model_id AS init_state_model, s.run_id AS init_state_run, s.cloned_from_model_id, s.clone_gate_kind,
  (SELECT count(*) FROM hydro.hydro_run r2 WHERE r2.model_id=f.new_id) AS runs_new,
  (SELECT min(cycle_time) FROM hydro.hydro_run r3 WHERE r3.model_id=f.old_id) AS old_first, (SELECT max(cycle_time) FROM hydro.hydro_run r4 WHERE r4.model_id=f.old_id) AS old_last
FROM first f LEFT JOIN hydro.state_snapshot s ON s.state_id=f.init_state_id ORDER BY 1;
SELECT count(*) FROM (VALUES (1)) x;
COMMIT;
SQL
)
```

Output (`1816-first-runs.txt`, verbatim). The trailing `count / 1 / (1 row)` is the sentinel statement
`SELECT count(*) FROM (VALUES (1)) x;`, which only proves the transaction reached its end:

```text
new_id|cycle_time|status|run_id|init_state_id|init_state_model|init_state_run|cloned_from_model_id|clone_gate_kind|runs_new|old_first|old_last
dg_0883c7e9c1006c6fd347df500315e9df|2026-08-23 00:00:00+00|published|fcst_gfs_2026082300_dg_0883c7e9c1006c6fd347df500315e9df|state_gfs_dg_0883c7e9c1006c6fd347df500315e9df_2026082300_gfs_2026082212_f012|||||67|2026-07-05 00:00:00+00|2026-08-22 12:00:00+00
dg_227f20efc981e3299b3e5bb2dfbf97a4|2026-08-23 12:00:00+00|published|fcst_ifs_2026082312_dg_227f20efc981e3299b3e5bb2dfbf97a4|state_IFS_dg_227f20efc981e3299b3e5bb2dfbf97a4_2026082312_ifs_2026082300_f012|||||66|2026-07-05 00:00:00+00|2026-08-23 00:00:00+00
dg_4d2ddab541048593d247d42da317fa3c|2026-08-23 12:00:00+00|superseded|fcst_ifs_2026082312_dg_4d2ddab541048593d247d42da317fa3c|state_IFS_dg_4d2ddab541048593d247d42da317fa3c_2026082312_ifs_2026082300_f012|||||58|2026-08-08 00:00:00+00|2026-08-23 00:00:00+00
dg_510fbd39433e9cd2ee948980efd523e1|2026-08-23 00:00:00+00|published|fcst_gfs_2026082300_dg_510fbd39433e9cd2ee948980efd523e1|state_gfs_dg_510fbd39433e9cd2ee948980efd523e1_2026082300_gfs_2026082212_f012|||||67|2026-07-05 00:00:00+00|2026-08-22 12:00:00+00
dg_5bd9935f3c32ac5b2936f68588489575|2026-08-23 00:00:00+00|published|fcst_gfs_2026082300_dg_5bd9935f3c32ac5b2936f68588489575|state_gfs_dg_5bd9935f3c32ac5b2936f68588489575_2026082300_gfs_2026082212_f012|||||67|2026-08-22 00:00:00+00|2026-08-22 12:00:00+00
dg_63f9fbca402ecb56e951d3c89a5100bf|2026-08-23 00:00:00+00|published|fcst_gfs_2026082300_dg_63f9fbca402ecb56e951d3c89a5100bf|state_gfs_dg_63f9fbca402ecb56e951d3c89a5100bf_2026082300_gfs_2026082212_f012|||||67|2026-07-05 00:00:00+00|2026-08-22 12:00:00+00
dg_67210bfe424cdcdc69aaa7469485a382|2026-08-23 12:00:00+00|published|fcst_ifs_2026082312_dg_67210bfe424cdcdc69aaa7469485a382|state_IFS_dg_67210bfe424cdcdc69aaa7469485a382_2026082312_ifs_2026082300_f012|||||66|2026-08-22 00:00:00+00|2026-08-23 00:00:00+00
dg_7e247591d8fae09dc8db71482c73efee|2026-08-23 12:00:00+00|published|fcst_ifs_2026082312_dg_7e247591d8fae09dc8db71482c73efee|state_IFS_dg_7e247591d8fae09dc8db71482c73efee_2026082312_ifs_2026082300_f012|||||66|2026-07-05 00:00:00+00|2026-08-23 00:00:00+00
dg_8543132517bba75279944a5960badcb7|2026-08-23 12:00:00+00|published|fcst_ifs_2026082312_dg_8543132517bba75279944a5960badcb7|state_IFS_dg_8543132517bba75279944a5960badcb7_2026082312_ifs_2026082300_f012|||||66|2026-07-05 00:00:00+00|2026-08-23 00:00:00+00
dg_95b0a3efb58fc525a1401d0c8f2b416f|2026-08-23 00:00:00+00|superseded|fcst_gfs_2026082300_dg_95b0a3efb58fc525a1401d0c8f2b416f|state_gfs_dg_95b0a3efb58fc525a1401d0c8f2b416f_2026082300_gfs_2026082212_f012|||||59|2026-08-08 00:00:00+00|2026-08-22 12:00:00+00
dg_9ccb261a39d51c24f4de9173fb4461b6|2026-08-23 12:00:00+00|published|fcst_ifs_2026082312_dg_9ccb261a39d51c24f4de9173fb4461b6|state_IFS_dg_9ccb261a39d51c24f4de9173fb4461b6_2026082312_ifs_2026082300_f012|||||66|2026-07-05 00:00:00+00|2026-08-23 00:00:00+00
dg_ba26785ea1e776f842e6916eb673a9ed|2026-08-23 12:00:00+00|published|fcst_ifs_2026082312_dg_ba26785ea1e776f842e6916eb673a9ed|state_IFS_dg_ba26785ea1e776f842e6916eb673a9ed_2026082312_ifs_2026082300_f012|||||66|2026-07-05 00:00:00+00|2026-08-23 00:00:00+00
dg_f6175cdb0f3825bec4807c386b5cbf38|2026-08-23 00:00:00+00|published|fcst_gfs_2026082300_dg_f6175cdb0f3825bec4807c386b5cbf38|state_gfs_dg_f6175cdb0f3825bec4807c386b5cbf38_2026082300_gfs_2026082212_f012|||||67|2026-07-05 00:00:00+00|2026-08-22 12:00:00+00
dg_f8d0ceab6c99000880b772ce138f7ed8|2026-08-23 00:00:00+00|published|fcst_gfs_2026082300_dg_f8d0ceab6c99000880b772ce138f7ed8|state_gfs_dg_f8d0ceab6c99000880b772ce138f7ed8_2026082300_gfs_2026082212_f012|||||67|2026-07-05 00:00:00+00|2026-08-22 12:00:00+00
(14 rows)
count
1
(1 row)
```

Clone rows, script `p-si.py` (node-27, NFS `index-last.json`):

```python
import json
NEW = """dg_f6175cdb0f3825bec4807c386b5cbf38 dg_8543132517bba75279944a5960badcb7 dg_292e1fd6e2d54fc3c800f6df07116d0e dg_07c146effd85828ee536d0033e2591b3 dg_5bd9935f3c32ac5b2936f68588489575 dg_67210bfe424cdcdc69aaa7469485a382 dg_0883c7e9c1006c6fd347df500315e9df dg_9ccb261a39d51c24f4de9173fb4461b6 dg_f8d0ceab6c99000880b772ce138f7ed8 dg_227f20efc981e3299b3e5bb2dfbf97a4 dg_95b0a3efb58fc525a1401d0c8f2b416f dg_4d2ddab541048593d247d42da317fa3c dg_510fbd39433e9cd2ee948980efd523e1 dg_7e247591d8fae09dc8db71482c73efee dg_63f9fbca402ecb56e951d3c89a5100bf dg_ba26785ea1e776f842e6916eb673a9ed""".split()
d = json.load(open("/home/ghdc/nwm/object-store/scheduler/state-index/index-last.json"))
ents = d.get("entries") if isinstance(d, dict) else d
if isinstance(ents, dict): ents = list(ents.values())
print("keys", list(d.keys())[:6] if isinstance(d, dict) else type(d), "entries", len(ents))
for m in NEW:
    rows = [e for e in ents if e.get("model_id") == m and e.get("cloned_from_model_id")]
    for e in rows:
        print(m, e.get("state_id"), e.get("valid_time"), e.get("cloned_from_model_id"), e.get("clone_gate_kind"), e.get("usable_flag"))
    if not rows: print(m, "NO CLONE ROW")
```

Output (`1816-clone-rows.txt`, verbatim; columns: new id, `state_id`, `valid_time`, `cloned_from_model_id`,
`clone_gate_kind`, `usable_flag`):

```text
keys ['checksum', 'entries', 'generated_at', 'schema_version'] entries 9950
dg_f6175cdb0f3825bec4807c386b5cbf38 state_gfs_dg_f6175cdb0f3825bec4807c386b5cbf38_2026082300_gfs_2026082212_f012 2026-08-23T00:00:00Z dg_d3da0d68ab5bb27b525f8d9e030b898d state_compatibility True
dg_8543132517bba75279944a5960badcb7 state_IFS_dg_8543132517bba75279944a5960badcb7_2026082312_ifs_2026082300_f012 2026-08-23T12:00:00Z dg_af93a48636397f87722bdbd11ceb99a3 state_compatibility True
dg_292e1fd6e2d54fc3c800f6df07116d0e state_gfs_dg_292e1fd6e2d54fc3c800f6df07116d0e_2026082300_gfs_2026082212_f012 2026-08-23T00:00:00Z dg_688c7bb90fd97da9befaee33cc428ff3 state_compatibility True
dg_07c146effd85828ee536d0033e2591b3 state_IFS_dg_07c146effd85828ee536d0033e2591b3_2026082312_ifs_2026082300_f012 2026-08-23T12:00:00Z dg_e404644e4705102949d42b5331ee6086 state_compatibility True
dg_5bd9935f3c32ac5b2936f68588489575 state_gfs_dg_5bd9935f3c32ac5b2936f68588489575_2026082300_gfs_2026082212_f012 2026-08-23T00:00:00Z dg_281ff8c7ccd761239bd39dc44977138f state_compatibility True
dg_67210bfe424cdcdc69aaa7469485a382 state_IFS_dg_67210bfe424cdcdc69aaa7469485a382_2026082312_ifs_2026082300_f012 2026-08-23T12:00:00Z dg_03b3cd97dec0e4847ed207347bc251a4 state_compatibility True
dg_0883c7e9c1006c6fd347df500315e9df state_gfs_dg_0883c7e9c1006c6fd347df500315e9df_2026082300_gfs_2026082212_f012 2026-08-23T00:00:00Z dg_4a3c03155c9e467ba667fbd69efb18be state_compatibility True
dg_9ccb261a39d51c24f4de9173fb4461b6 state_IFS_dg_9ccb261a39d51c24f4de9173fb4461b6_2026082312_ifs_2026082300_f012 2026-08-23T12:00:00Z dg_9d318f475991068cb3d72d0f3dea4ba5 state_compatibility True
dg_f8d0ceab6c99000880b772ce138f7ed8 state_gfs_dg_f8d0ceab6c99000880b772ce138f7ed8_2026082300_gfs_2026082212_f012 2026-08-23T00:00:00Z dg_98d20f5c16f05bbe7e1f9f72234742fd state_compatibility True
dg_227f20efc981e3299b3e5bb2dfbf97a4 state_IFS_dg_227f20efc981e3299b3e5bb2dfbf97a4_2026082312_ifs_2026082300_f012 2026-08-23T12:00:00Z dg_cdab38c99bf9fd1fdb6fa9e95466b5ac state_compatibility True
dg_95b0a3efb58fc525a1401d0c8f2b416f state_gfs_dg_95b0a3efb58fc525a1401d0c8f2b416f_2026082300_gfs_2026082212_f012 2026-08-23T00:00:00Z dg_2a26a183d131ce80987dbff37d994839 state_compatibility True
dg_4d2ddab541048593d247d42da317fa3c state_IFS_dg_4d2ddab541048593d247d42da317fa3c_2026082312_ifs_2026082300_f012 2026-08-23T12:00:00Z dg_5b6d9f6a10a34f2e703a36f0312dccb9 state_compatibility True
dg_510fbd39433e9cd2ee948980efd523e1 state_gfs_dg_510fbd39433e9cd2ee948980efd523e1_2026082300_gfs_2026082212_f012 2026-08-23T00:00:00Z dg_f14b54044d10944d60d66a0acca4d409 state_compatibility True
dg_7e247591d8fae09dc8db71482c73efee state_IFS_dg_7e247591d8fae09dc8db71482c73efee_2026082312_ifs_2026082300_f012 2026-08-23T12:00:00Z dg_04405f8ced65fec0aae4d56d3d31a850 state_compatibility True
dg_63f9fbca402ecb56e951d3c89a5100bf state_gfs_dg_63f9fbca402ecb56e951d3c89a5100bf_2026082300_gfs_2026082212_f012 2026-08-23T00:00:00Z dg_e5344485e0e71079165927cda2046233 state_compatibility True
dg_ba26785ea1e776f842e6916eb673a9ed state_IFS_dg_ba26785ea1e776f842e6916eb673a9ed_2026082312_ifs_2026082300_f012 2026-08-23T12:00:00Z dg_a9ec352ed3a850645dd9fe965bc8dd7e state_compatibility True
```

Cross-check of the three captured files against each other, `p-crosscheck.py` (run locally over the captured
files, no network):

```python
"""Cross-check the captured #1816 evidence files against each other (local, no network)."""
rows = [l.split("\t") for l in open("1816-mapping.txt") if l.startswith("basins_")]
old_of = {r[4]: r[2] for r in rows}
first = {}
for l in open("1816-first-runs.txt"):
    f = l.rstrip("\n").split("|")
    if f[0].startswith("dg_"):
        first[f[0]] = f
clone = {}
for l in open("1816-clone-rows.txt"):
    f = l.split()
    if f and f[0].startswith("dg_"):
        clone[f[0]] = f
print("mapping rows", len(rows), "first-run rows", len(first), "clone rows", len(clone))
for r in rows:
    new = r[4]
    c = clone.get(new)
    fr = first.get(new)
    clone_ok = c is not None and c[3] == old_of[new] and c[4] == "state_compatibility" and c[5] == "True"
    if fr is None:
        print(r[0], r[1], new[:11], "clone_from_old", clone_ok, "first_forecast_run NONE")
        continue
    init_eq = fr[4] == c[1]
    valid_eq = c[2].replace("T", " ").replace("Z", "+00") == fr[1]
    snap_null = fr[5:9] == ["", "", "", ""]
    print(r[0], r[1], new[:11], "clone_from_old", clone_ok, "init_state_id==clone.state_id", init_eq,
          "clone.valid_time==cycle", valid_eq, "state_snapshot_join_null", snap_null, "status", fr[2], "runs", fr[9])
```

Output:

```text
mapping rows 16 first-run rows 14 clone rows 16
basins_heihe gfs dg_f6175cdb clone_from_old True init_state_id==clone.state_id True clone.valid_time==cycle True state_snapshot_join_null True status published runs 67
basins_heihe ifs dg_85431325 clone_from_old True init_state_id==clone.state_id True clone.valid_time==cycle True state_snapshot_join_null True status published runs 66
basins_hetianhe gfs dg_292e1fd6 clone_from_old True first_forecast_run NONE
basins_hetianhe ifs dg_07c146ef clone_from_old True first_forecast_run NONE
basins_huai_main gfs dg_5bd9935f clone_from_old True init_state_id==clone.state_id True clone.valid_time==cycle True state_snapshot_join_null True status published runs 67
basins_huai_main ifs dg_67210bfe clone_from_old True init_state_id==clone.state_id True clone.valid_time==cycle True state_snapshot_join_null True status published runs 66
basins_qhh gfs dg_0883c7e9 clone_from_old True init_state_id==clone.state_id True clone.valid_time==cycle True state_snapshot_join_null True status published runs 67
basins_qhh ifs dg_9ccb261a clone_from_old True init_state_id==clone.state_id True clone.valid_time==cycle True state_snapshot_join_null True status published runs 66
basins_qinyijiang gfs dg_f8d0ceab clone_from_old True init_state_id==clone.state_id True clone.valid_time==cycle True state_snapshot_join_null True status published runs 67
basins_qinyijiang ifs dg_227f20ef clone_from_old True init_state_id==clone.state_id True clone.valid_time==cycle True state_snapshot_join_null True status published runs 66
basins_shj_2shj gfs dg_95b0a3ef clone_from_old True init_state_id==clone.state_id True clone.valid_time==cycle True state_snapshot_join_null True status superseded runs 59
basins_shj_2shj ifs dg_4d2ddab5 clone_from_old True init_state_id==clone.state_id True clone.valid_time==cycle True state_snapshot_join_null True status superseded runs 58
basins_tailanhe gfs dg_510fbd39 clone_from_old True init_state_id==clone.state_id True clone.valid_time==cycle True state_snapshot_join_null True status published runs 67
basins_tailanhe ifs dg_7e247591 clone_from_old True init_state_id==clone.state_id True clone.valid_time==cycle True state_snapshot_join_null True status published runs 66
basins_weiganhe gfs dg_63f9fbca clone_from_old True init_state_id==clone.state_id True clone.valid_time==cycle True state_snapshot_join_null True status published runs 67
basins_weiganhe ifs dg_ba26785e clone_from_old True init_state_id==clone.state_id True clone.valid_time==cycle True state_snapshot_join_null True status published runs 66
```

## 3. Exceptions (observations, not causes)

Neither exception is a cold start. Of the 16 new ids, 12 are in the current manifest (`1816-new-in-current: 12`
below); the other four are these two basins.

- **hetianhe** (`dg_292e1fd6e2d54fc3c800f6df07116d0e` gfs, `dg_07c146effd85828ee536d0033e2591b3` ifs): both have a
  `state_compatibility` clone row from their predecessors, but neither has a `forecast` `hydro_run` row (absent from
  the 14 rows above). The backup `manifest-last.json.pre-hhe-subs-20260825` (`generated_at` 2026-08-25T02:25:14Z)
  already carries different ids, `dg_9d9432bd2f76358e4c6caece38b6872c` and `dg_2e09e66398fe4148eafcbbbf875171b4`,
  which are still the current hetianhe ids. The `republish-1832/` directory holds `baseline-registry.json` and
  `dg-candidate.json` and no `provision-receipt.json` (the `None` line). Why the #1816 ids never ran is not
  established by this evidence.
  Follow-up evaluation (#2365 AC3): no new issue. Both ids are out of the current manifest and were replaced by the
  ids the #1832 republish carries, so nothing live consumes them; the only open question is historical.
- **shj_2shj** (`dg_95b0a3efb58fc525a1401d0c8f2b416f` gfs, `dg_4d2ddab541048593d247d42da317fa3c` ifs): the first
  admitted runs started from their clone rows like the other twelve (59 / 58 runs, first-run status now
  `superseded`). The basin later left the manifest (`current for basin: []`).

Scripts `p-ht2.py` and `p-ht3.py` (node-27, NFS registry):

```python
import json, glob, os
R="/home/ghdc/nwm/object-store/scheduler/"
d=json.load(open(R+"registry/manifest-last.json"))
print("current", d["generated_at"], [(m["model_id"], m["active_flag"], m["lifecycle_state"], m["package_checksum"][:16]) for m in d["models"] if m["basin_id"]=="basins_hetianhe"])
print("1816-new-in-current:", sorted({m["model_id"] for m in d["models"]} & set("""dg_f6175cdb0f3825bec4807c386b5cbf38 dg_8543132517bba75279944a5960badcb7 dg_292e1fd6e2d54fc3c800f6df07116d0e dg_07c146effd85828ee536d0033e2591b3 dg_5bd9935f3c32ac5b2936f68588489575 dg_67210bfe424cdcdc69aaa7469485a382 dg_0883c7e9c1006c6fd347df500315e9df dg_9ccb261a39d51c24f4de9173fb4461b6 dg_f8d0ceab6c99000880b772ce138f7ed8 dg_227f20efc981e3299b3e5bb2dfbf97a4 dg_95b0a3efb58fc525a1401d0c8f2b416f dg_4d2ddab541048593d247d42da317fa3c dg_510fbd39433e9cd2ee948980efd523e1 dg_7e247591d8fae09dc8db71482c73efee dg_63f9fbca402ecb56e951d3c89a5100bf dg_ba26785ea1e776f842e6916eb673a9ed""".split())).__len__())
print(os.listdir(R+"republish-1832"))
p=json.load(open(R+"republish-1832/provision-receipt.json")) if os.path.exists(R+"republish-1832/provision-receipt.json") else None
print(json.dumps(p)[:1500] if p else None)
for f in sorted(glob.glob(R+"registry/manifest-last*")):
    try:
        x=json.load(open(f)); hs=[m["model_id"] for m in x["models"] if m["basin_id"]=="basins_hetianhe"]
        print(os.path.basename(f), x["generated_at"], hs)
    except Exception as e: print(os.path.basename(f), "ERR", e)
```

```python
import json
R="/home/ghdc/nwm/object-store/scheduler/registry/"
NEW=dict(x.split(":") for x in """basins_heihe/gfs:dg_f6175cdb0f3825bec4807c386b5cbf38 basins_heihe/ifs:dg_8543132517bba75279944a5960badcb7 basins_hetianhe/gfs:dg_292e1fd6e2d54fc3c800f6df07116d0e basins_hetianhe/ifs:dg_07c146effd85828ee536d0033e2591b3 basins_huai_main/gfs:dg_5bd9935f3c32ac5b2936f68588489575 basins_huai_main/ifs:dg_67210bfe424cdcdc69aaa7469485a382 basins_qhh/gfs:dg_0883c7e9c1006c6fd347df500315e9df basins_qhh/ifs:dg_9ccb261a39d51c24f4de9173fb4461b6 basins_qinyijiang/gfs:dg_f8d0ceab6c99000880b772ce138f7ed8 basins_qinyijiang/ifs:dg_227f20efc981e3299b3e5bb2dfbf97a4 basins_shj_2shj/gfs:dg_95b0a3efb58fc525a1401d0c8f2b416f basins_shj_2shj/ifs:dg_4d2ddab541048593d247d42da317fa3c basins_tailanhe/gfs:dg_510fbd39433e9cd2ee948980efd523e1 basins_tailanhe/ifs:dg_7e247591d8fae09dc8db71482c73efee basins_weiganhe/gfs:dg_63f9fbca402ecb56e951d3c89a5100bf basins_weiganhe/ifs:dg_ba26785ea1e776f842e6916eb673a9ed""".split())
cur=json.load(open(R+"manifest-last.json"))["models"]
ids={m["model_id"] for m in cur}
for k,v in NEW.items():
    if v not in ids:
        b=k.split("/")[0]; print(k, v, "NOT CURRENT; current for basin:", [m["model_id"] for m in cur if m["basin_id"]==b])
```

Output (`1816-hetianhe-shj2shj.txt`, verbatim; the first four lines are `p-ht2.py`'s current hetianhe rows, the
12-id count, the `republish-1832/` listing and the absent provision receipt; then its per-backup hetianhe ids; the
last four lines are `p-ht3.py`):

```text
current 2026-09-26T02:42:30.372371Z [('dg_2e09e66398fe4148eafcbbbf875171b4', True, 'active', '14219ae40fad5bd3'), ('dg_9d9432bd2f76358e4c6caece38b6872c', True, 'active', 'c7cdfc060e2d23ad')]
1816-new-in-current: 12
['baseline-registry.json', 'dg-candidate.json']
None
manifest-last.20260824T225718Z.bak.json 2026-08-24T06:24:35.432773Z ['dg_292e1fd6e2d54fc3c800f6df07116d0e', 'dg_07c146effd85828ee536d0033e2591b3']
manifest-last.json 2026-09-26T02:42:30.372371Z ['dg_2e09e66398fe4148eafcbbbf875171b4', 'dg_9d9432bd2f76358e4c6caece38b6872c']
manifest-last.json.bak-hhe-retire-20260807 2026-08-06T02:27:56.018204Z ['dg_688c7bb90fd97da9befaee33cc428ff3', 'dg_e404644e4705102949d42b5331ee6086']
manifest-last.json.bak-neiliuqu-retire-20260825 2026-08-25T12:18:13.896900Z ['dg_9d9432bd2f76358e4c6caece38b6872c', 'dg_2e09e66398fe4148eafcbbbf875171b4']
manifest-last.json.bak-onboard-1699-20260822 2026-08-22T07:12:33.036072Z ['dg_688c7bb90fd97da9befaee33cc428ff3', 'dg_e404644e4705102949d42b5331ee6086']
manifest-last.json.bak-recal-1698-20260822 2026-08-22T02:32:56.039536Z ['dg_688c7bb90fd97da9befaee33cc428ff3', 'dg_e404644e4705102949d42b5331ee6086']
manifest-last.json.bak-zhaochen-retire-20260825 2026-08-25T10:57:03.607588Z ['dg_9d9432bd2f76358e4c6caece38b6872c', 'dg_2e09e66398fe4148eafcbbbf875171b4']
manifest-last.json.pre-delivery-20260826 2026-08-26T02:34:50.407043Z ['dg_9d9432bd2f76358e4c6caece38b6872c', 'dg_2e09e66398fe4148eafcbbbf875171b4']
manifest-last.json.pre-direct-grid-20260718T044328Z 2026-07-17T16:23:53.214741Z ['basins_hetianhe_shud']
manifest-last.json.pre-hhe-subs-20260825 2026-08-25T02:25:14.026230Z ['dg_9d9432bd2f76358e4c6caece38b6872c', 'dg_2e09e66398fe4148eafcbbbf875171b4']
manifest-last.json.pre-hlj-threads-20260922-1209Z 2026-09-22T04:21:41.393930Z ['dg_2e09e66398fe4148eafcbbbf875171b4', 'dg_9d9432bd2f76358e4c6caece38b6872c']
manifest-last.json.pre-jialingjiang-sourcepath-20260914T171300Z 2026-09-14T02:18:56.651514Z ['dg_2e09e66398fe4148eafcbbbf875171b4', 'dg_9d9432bd2f76358e4c6caece38b6872c']
manifest-last.json.pre-ksath2-20260826 2026-08-25T15:29:07.880411Z ['dg_9d9432bd2f76358e4c6caece38b6872c', 'dg_2e09e66398fe4148eafcbbbf875171b4']
manifest-last.json.pre-onboarding-19-20260922-0249Z 2026-09-22T02:43:20.939364Z ['dg_2e09e66398fe4148eafcbbbf875171b4', 'dg_9d9432bd2f76358e4c6caece38b6872c']
manifest-last.json.pre-onboarding-19-rename-20260922-0420Z 2026-09-22T03:02:24.556319Z ['dg_2e09e66398fe4148eafcbbbf875171b4', 'dg_9d9432bd2f76358e4c6caece38b6872c']
manifest-last.json.pre-onboarding-new9-20260901-0227Z 2026-08-28T03:22:39.994002Z ['dg_2e09e66398fe4148eafcbbbf875171b4', 'dg_9d9432bd2f76358e4c6caece38b6872c']
manifest-last.json.pre-republish-1816-20260824T062347Z 2026-08-24T02:23:33.023600Z ['dg_688c7bb90fd97da9befaee33cc428ff3', 'dg_e404644e4705102949d42b5331ee6086']
basins_hetianhe/gfs dg_292e1fd6e2d54fc3c800f6df07116d0e NOT CURRENT; current for basin: ['dg_2e09e66398fe4148eafcbbbf875171b4', 'dg_9d9432bd2f76358e4c6caece38b6872c']
basins_hetianhe/ifs dg_07c146effd85828ee536d0033e2591b3 NOT CURRENT; current for basin: ['dg_2e09e66398fe4148eafcbbbf875171b4', 'dg_9d9432bd2f76358e4c6caece38b6872c']
basins_shj_2shj/gfs dg_95b0a3efb58fc525a1401d0c8f2b416f NOT CURRENT; current for basin: []
basins_shj_2shj/ifs dg_4d2ddab541048593d247d42da317fa3c NOT CURRENT; current for basin: []
```

## 4. Re-running

All reads are side-effect free. The scripts are not checked in: save the fenced blocks above under their names
(`p-hr2.sh`, `p-manifest.py`, ...) first. Re-run form (not a record of the exact invocation used on 2026-09-26):

```bash
ssh -p 32099 nwm@210.77.77.27 'bash -s' < p-hr2.sh
ssh -p 32099 nwm@210.77.77.27 'cd /home/nwm/NWM && PATH=$HOME/.local/bin:$PATH uv run --no-sync python -' < p-manifest.py
```

The manifests keep changing (this capture's current `manifest-last.json` is `generated_at`
2026-09-26T02:42:30.372371Z), so the "current" lines of section 3 describe that date, while sections 1 and 2 read
fixed backups and fixed history.
