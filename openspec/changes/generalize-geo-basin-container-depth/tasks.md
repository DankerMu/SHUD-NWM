# Tasks

## Risk Triage

```text
Issue type: refactor
Project profile: SHUD
Blast radius: medium
Fixture level: expanded
Upstream suggested level: absent (override: 无上游建议；命中 mandatory expanded 触发词 path / 外部数据发现，
  以及 project-profile 的 geospatial / shapefile / basin geometry 域触发词)
Why:
- 从 Basins root 做外部数据发现，输出驱动公网地图图层 id
- 目录名 -> basin_id 的路径推导，失配是静默错图层而非报错
- 双份逐字节相同实现，改一份漏一份就是不对称
Selected risk packs:
- File IO / path safety / overwrite
- Resource limits / large input / discovery
- Legacy compatibility / examples
OpenSpec change: generalize-geo-basin-container-depth (generated)
Evidence floor:
- uv run pytest -q tests/test_national_geo_scripts.py
- uv run ruff check scripts/geo tests/test_national_geo_scripts.py
- grep 两份脚本 zhaochen 字面量为 0
```

## Fixture Content

```text
Change surface:
- scripts/geo/build_national_domain_geo.py::_discover_basin_gis_dirs
- scripts/geo/build_national_river_geo.py::_discover_basin_gis_dirs
- tests/test_national_geo_scripts.py
Must preserve:
- depth-1 布局产出 parts[0]（hetianhe、qinyijiang 等 15 个流域）
- zhaochen/BST -> zhaochen_bst（已提交 geojson 产物的 id 不因本 PR 改变）
- 返回值仍是 sorted、(name, gis_dir) 二元组列表
- _named_basin_gis_dir / _basin_id / CLI 参数不变
Must add/change:
- 容器深度由 input 下标（len(parts)-4）推导，删除 zhaochen 字面量
- depth-2 通用折叠：f"{parts[0]}_{parts[1].lower()}"
- depth >= 3 跳过（原为静默取 parts[0]）
Seams under test:
- _discover_basin_gis_dirs(basins_root) 的返回列表——两份脚本各自独立断言
Risk packs:
- Public API / CLI / script entry: not selected - CLI 参数与 main() 未改，仅内部发现函数
- File IO / path safety / overwrite: selected - 相对路径分段推导 basin_id，误折叠导致图层互相覆盖
- Schema / columns / units / field names: not selected - GeoJSON 属性集不变
- Legacy compatibility / examples: selected - zhaochen 现有 id 必须逐字节不变
- Resource limits / large input / discovery: selected - glob 发现，深度规则决定收录/跳过
- Error handling / rollback / partial outputs: not selected - 纯读发现，无写、无回滚
- Evidence / JSON / schema ingestion: not selected - 不解析外部 JSON
Required evidence:
- tests/test_national_geo_scripts.py 覆盖 depth-1 / 通用 depth-2 / zhaochen 回归 / depth-3 跳过 /
  **同容器双兄弟碰撞**（`HYS/BST` 与 `HYS/MC` 必须产出两个不同 basin_name；折叠规则缺失时两者都退化成 `HYS`，
  在 main() 的 dict 合并处静默互相覆盖——这是本变更要钉死的失效模式）
Non-goals:
- 不改 _basin_id 的 .lower() 与规范 _slug_id 的分歧（design D3，report don't fix）
- 不改 _named_basin_gis_dir（design D4）
- 不重建 apps/frontend/public/geo/*.geojson 产物
- 不做 #1701 的目录改名 / publish / 克隆 / manifest / seed（运维 cutover 窗口）
```

## Tasks

- [x] 1. 两份脚本的 `_discover_basin_gis_dirs` 改为 input 下标推导深度，删 `zhaochen` 字面量
- [x] 2. `tests/test_national_geo_scripts.py` 补五类断言（含 depth-3 跳过与同容器双兄弟碰撞）
- [x] 3. `uv run pytest -q tests/test_national_geo_scripts.py` 绿
- [x] 4. `uv run ruff check scripts/geo tests/test_national_geo_scripts.py` 绿
