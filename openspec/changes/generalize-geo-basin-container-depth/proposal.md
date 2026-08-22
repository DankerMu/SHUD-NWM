# Generalize geo basin container-depth discovery

## Why

`scripts/geo/build_national_domain_geo.py` 与 `scripts/geo/build_national_river_geo.py` 的
`_discover_basin_gis_dirs()` 用 `if basin_name == "zhaochen"` 写死唯一一个二级容器目录名，
才把 `zhaochen/BST/input/BST/gis/` 折成 `zhaochen_bst`。

#1701 要把该容器目录从 `zhaochen/` 改名为 `HYS/`。硬编码一旦失配，`HYS/{BST,MC,WEM}` 三个子流域
会全部退化成同一个 `basin_name = "HYS"`，在同一 `basin_id` 下互相覆盖——不是报错，是静默产出错图层。

`workers/model_registry/basins_discovery._find_model_dirs()` 早就有通用的 depth-1/depth-2 规则；
两份 geo 脚本是同一 Basins root 上的第二套发现实现，应对齐而不是各自写死目录名。

## What

把两份脚本的容器判定改成**由 `input/` 所在层级推导**的通用规则，删掉 `zhaochen` 字面量；
depth-3 及更深不再静默取 `parts[0]`，改为跳过（与 `_find_model_dirs` 只走 depth-1/2 一致）。
加单测覆盖四种布局 + 折叠缺失时的碰撞失效模式。

## Scope

仅 #1701 步骤 2（去硬编码 + 单测）。#1701 的目录改名、baseline publish、克隆行、manifest 发布、
seed、geo 图层重建属运维 cutover 窗口，不在本 PR。
