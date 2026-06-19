# qhh-sample fixture

Minimal SHUD input package subset extracted from node-27 `/home/nwm/NWM/data/Basins/qhh/input/qhh/` for unit-test fixtures backing [openspec change `feat-reach-geom-from-river-shp`](../../../../openspec/changes/feat-reach-geom-from-river-shp/proposal.md) (issues #558–#566).

## Source

- Host: node-27 `nwm@210.77.77.27:32099`
- Path: `/home/nwm/NWM/data/Basins/qhh/input/qhh/`
- Basin: qhh (Qinghai Lake)
- Source SHUD package mtime: 2026-05-14 17:13 (matches [`docs/runbooks/qhh-22-business-bringup.md`](../../../../docs/runbooks/qhh-22-business-bringup.md) baseline)
- Extracted on: 2026-06-19 via GDAL 3.4.1 (`ogr2ogr` schema-preserving copy)

## Sampling rule

Selected reach `Index` values: `{1, 2, 3, 9, 180}`. Chosen because:

| Index | Rationale |
|---|---|
| 1, 2, 3 | Smallest consecutive reach indices — exercise lexicographic-order ID generation (`_reach_000001`, `_reach_000002`, `_reach_000003`) and downstream resolution (reach 1 → Down=2, reach 2 → Down=180, reach 3 → Down=4 [not in subset → terminal-like in fixture context]) |
| 9 | First reach with multi-part `seg.shp` records (iRiv=9 has 4 multi-part segments) — fixture preserves at least one multi-part to evidence the source-data issue this change fixes |
| 180 | Downstream chain target of reach 2 — exercises `<model_id>_reach_000180` lookup via crosswalk |

## Contents

```
qhh-sample/
├── gis/
│   ├── river.{shp,dbf,shx,prj}   5 records, 0 multi-part (single-part flow-ordered polylines)
│   └── seg.{shp,dbf,shx,prj}     18 records, 4 multi-part (preserves seg.shp design flaw for evidence)
├── qhh.sp.riv                    5 reach rows + 2-line header
└── qhh.sp.rivseg                 18 segment rows + 2-line header
```

### `river.shp` schema

15 dbf fields preserved: `Index, Down, Type, Slope, Length, BC, Index_1, Depth, BankSlope, Width, Sinuosity, Manning, Cwr, KsatH, BedThick`.

Note: `Index_1` is an artefact of the original SHUD pre-processing pipeline
(rSHUD R scripts left a duplicate `Index` field with `_1` suffix); it is
preserved verbatim from the source. Tests that validate the dbf field
invariant should treat it as ignorable, OR codify it as a known extra field.
The required field set per spec is
`{Index, Down, Type, Slope, Length, BC, Depth, BankSlope, Width, Sinuosity,
Manning, Cwr, KsatH, BedThick}` (14 fields, excluding `Index_1`).

### `seg.shp` schema

2 dbf fields: `iRiv`, `iEle`. This is the SHUD segment → mesh element index — confirming that `seg.shp` is **not** intended as a display polyline source (see [proposal.md](../../../../openspec/changes/feat-reach-geom-from-river-shp/proposal.md) Why).

## Verification (run locally)

```bash
python3 <<'EOF'
from osgeo import ogr
for shp, expected_count, expected_multi in [
    ('tests/fixtures/basins/qhh-sample/gis/river.shp', 5, 0),
    ('tests/fixtures/basins/qhh-sample/gis/seg.shp', 18, 4),
]:
    ds = ogr.Open(shp)
    layer = ds.GetLayer(0)
    total = multi = 0
    for f in layer:
        g = f.GetGeometryRef()
        if g is None: continue
        total += 1
        if g.GetGeometryName().startswith('MULTI') and g.GetGeometryCount() > 1:
            multi += 1
    assert total == expected_count and multi == expected_multi, f'{shp}: {total}/{multi}'
print('fixture invariants OK')
EOF
```

## License / handling

This fixture is a derivative of SHUD model input that is **internal** to the NHMS/NWM project. Do not redistribute. CLAUDE.md "环境隔离原则" excludes `data/Basins/` from git sync; **this subset under `tests/fixtures/` is the only sanctioned committed copy** for unit-test reproducibility.

## Re-extraction (if source data updates)

```bash
ssh -p 32099 nwm@210.77.77.27 \
  'cd /home/nwm/NWM/data/Basins/qhh/input/qhh/gis/ && \
   python3 -c "<see commit message for full GDAL Python schema-preserving copy script>"' \
  && scp -P 32099 nwm@210.77.77.27:/tmp/qhh-sample-fixture.tar.gz /tmp/ \
  && tar xzf /tmp/qhh-sample-fixture.tar.gz -C tests/fixtures/basins/qhh-sample/
```
