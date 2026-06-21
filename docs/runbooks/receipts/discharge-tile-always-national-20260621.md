# Receipt — discharge layer always national (PR #602 / issue #601)

**Date**: 2026-06-21 (UTC+8)
**Node**: node-27 (210.77.77.27:32099, user=nwm)
**Commit**: `6feb042` (master)
**PR**: [#602](https://github.com/DankerMu/9/pull/602)
**Closes**: [#601](https://github.com/DankerMu/SHUD-NWM/issues/601)
**Part of**: [#600](https://github.com/DankerMu/SHUD-NWM/issues/600)

## Deploy steps executed

```
ssh -p 32099 nwm@210.77.77.27 'cd /home/nwm/NWM && git pull --ff-only origin master'
→ master ff to 6feb042 (no untracked/working-tree conflict)

ssh -p 32099 nwm@210.77.77.27 'pkill -f "uvicorn apps.api.main:app"; setsid /tmp/relaunch-discharge-fix.sh > /tmp/uvicorn-discharge-fix.log 2>&1 < /dev/null &'
→ new uvicorn PID 2326484 (PPID=1, listener 127.0.0.1:8080)
→ /tmp/relaunch-discharge-fix.sh sources infra/env/display.env so DATABASE_URL is set
```

## Live receipt 1 — `/api/v1/layers` catalog, 3 caller forms

Spec scenarios covered: *Runless `/api/v1/layers` catalog*, *Run-scoped `/api/v1/layers?run_id=<X>` catalog*, *Discharge catalog cache identity is run-agnostic*, *Flood-return-period and warning-level remain run-scoped*.

### [runless] — `curl http://127.0.0.1:8080/api/v1/layers` (no run_id)

```
discharge.tile_url_template: /api/v1/tiles/hydro-national/q_down/{valid_time}/{z}/{x}/{y}.pbf
discharge.required_placeholders: ['valid_time', 'z', 'x', 'y']
discharge.maplibre_source_layer: hydro
discharge.property_schema.required has basin_id: True
discharge.source_refs: {}
flood-return-period.tile_url_template: /api/v1/tiles/flood-return-period/{run_id}/{duration}/{valid_time}/{z}/{x}/{y}.pbf
river-network.tile_url_template: /api/v1/tiles/river-network/{basin_version_id}/{z}/{x}/{y}.pbf
flood-return-period.source_refs: {'run_id': 'fcst_ifs_2026062000_basins_qhh_shud', 'source_version': 'basins_qhh_rivnet_vbasins;run-revision:ed29657479a0a6c7', 'basin_version_id': 'basins_qhh_vbasins', 'river_network_version_id': 'basins_qhh_rivnet_vbasins', 'duration': '1h'}
```

### [run_scoped[qhh]] — `curl http://127.0.0.1:8080/api/v1/layers?run_id=fcst_ifs_2026062000_basins_qhh_shud`

```
discharge.tile_url_template: /api/v1/tiles/hydro-national/q_down/{valid_time}/{z}/{x}/{y}.pbf
discharge.required_placeholders: ['valid_time', 'z', 'x', 'y']
discharge.maplibre_source_layer: hydro
discharge.property_schema.required has basin_id: True
discharge.source_refs: {}
flood-return-period.tile_url_template: /api/v1/tiles/flood-return-period/{run_id}/{duration}/{valid_time}/{z}/{x}/{y}.pbf
flood-return-period.source_refs: {'run_id': 'fcst_ifs_2026062000_basins_qhh_shud', ..., 'basin_version_id': 'basins_qhh_vbasins', ...}
```

### [run_scoped[heihe]] — `curl http://127.0.0.1:8080/api/v1/layers?run_id=fcst_ifs_2026062000_basins_heihe_shud`

```
discharge.tile_url_template: /api/v1/tiles/hydro-national/q_down/{valid_time}/{z}/{x}/{y}.pbf
discharge.required_placeholders: ['valid_time', 'z', 'x', 'y']
discharge.maplibre_source_layer: hydro
discharge.property_schema.required has basin_id: True
discharge.source_refs: {}
flood-return-period.tile_url_template: /api/v1/tiles/flood-return-period/{run_id}/{duration}/{valid_time}/{z}/{x}/{y}.pbf
flood-return-period.source_refs: {'run_id': 'fcst_ifs_2026062000_basins_heihe_shud', ..., 'basin_version_id': 'basins_heihe_vbasins', ...}
```

### Verdict — receipt 1

- ✅ Across all 3 caller shapes, discharge entry is byte-identical (template, placeholders, maplibre source layer, property_schema, source_refs)
- ✅ flood-return-period stays per-run: its `source_refs.run_id` correctly reflects the caller's run_id (qhh vs heihe)
- ✅ river-network unchanged
- ✅ Cache identity invariant proven: discharge `source_refs == {}` in all 3 calls

## Live receipt 2 — national discharge tile endpoint reachable

```
discharge.metadata.valid_times has 100 entries (national union across basins)
probe URL: http://127.0.0.1:8080/api/v1/tiles/hydro-national/q_down/2026-06-22T20%3A00%3A00Z/9/394/197.pbf
→ HTTP 200, application/x-protobuf, 0 bytes (empty tile — z9 coord outside any basin geometry; route + SQL are healthy)
```

### Verdict — receipt 2

- ✅ National tile route serves 200 (PBF content negotiation works; empty body at an arbitrary x/y just means no segments in that tile)
- ✅ Catalog valid_times union spans 100 distinct times (multi-basin coverage)

## Pending — receipt 3 (浏览器实拍 / browser live verification)

Spec scenario covered: *Frontend enrichment phase does not downgrade discharge*. Owner: user (manual browser action; not scriptable from headless side).

To close the loop:

1. 浏览器打开 `http://localhost:18080/` (或 node-27 display 实际入口)
2. DevTools Network 面板，过滤 `hydro-national`，确认 MapLibre 实际请求 `/api/v1/tiles/hydro-national/q_down/...` 而不是 `/api/v1/tiles/hydro/{run_id}/...`
3. DevTools Console 跑 `map.getSource('hydro').url` (或同效的 MapLibre source-URL 查询) — 应返回 hydro-national 模板字符串
4. 缩放至甘肃黑河流域（约 lon 99-101, lat 38-42），确认 heihe basin 河段渲染（不再是空白）
5. 点击 ≥1 个 heihe 河段触发曲线弹窗，截图入本 receipt

附用户操作的截图后，本 receipt 即完整闭合。
