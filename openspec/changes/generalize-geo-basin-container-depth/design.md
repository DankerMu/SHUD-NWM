# Design

## 现状

两份脚本各有一份逐字节相同的 `_discover_basin_gis_dirs()`：

```python
parts = relative.parts                      # 相对 basins_root
if len(parts) < 5 or parts[-4] != "input" or parts[-2] != "gis":
    continue
basin_name = parts[0]
if basin_name == "zhaochen" and len(parts) >= 6:
    basin_name = f"{parts[0]}_{parts[1].lower()}"
```

glob 是 `**/input/*/gis/{domain,river}.shp`，所以尾四段恒为 `input/<shud_input_name>/gis/<x>.shp`，
`input` 的下标恒等于 `len(parts) - 4`。该下标就是容器深度：

| `len(parts)-4` | 布局 | 期望 basin_name |
|---|---|---|
| 0 | `input/<x>/gis/`（`**` 匹配零层，直接落在 root 下） | 无——无目录名可用 |
| 1 | `hetianhe/input/hetian9000-2/gis/` | `hetianhe` |
| 2 | `zhaochen/BST/input/BST/gis/` | `zhaochen_bst` |
| ≥3 | 更深 | 无对应注册身份 |

depth-0 今天已被 `len(parts) < 5` 挡掉，新规则下落入同一个 `else: continue`，行为不变。

现有代码只对字面量 `zhaochen` 走 depth-2 分支，其它任何 depth-2 或更深布局都退化成 `parts[0]`。

## 决策

### D1：深度由 `input` 下标推导，不再匹配目录名

```python
input_index = len(parts) - 4
if input_index == 1:
    basin_name = parts[0]
elif input_index == 2:
    basin_name = f"{parts[0]}_{parts[1].lower()}"
else:
    continue
```

与 `basins_discovery._find_model_dirs()` 的 depth-1/depth-2 语义一致。
`zhaochen/BST` 仍产出 `zhaochen_bst`（现有已提交产物不变），`HYS/BST` 产出 `HYS_bst`
→ `_basin_id()` → `basins_hys_bst`，正是 #1701 要的 id。

### D2：depth ≥ 3 改为跳过（行为变更，蓄意）

今天 depth-3 会静默返回 `parts[0]`，把互不相关的模型塞进同一 `basin_id`。
`_find_model_dirs` 不走 depth-3，即这类目录在注册表里根本没有身份；
产出一个注册表里不存在的 `basin_id` 图层比不产出更坏。故跳过，且立测试钉死。

### D3：不动 `_basin_id()`（report, don't fix）

`_basin_id(name) = f"basins_{name.lower()}"` 与规范 `_slug_id()`
（`re.sub(r"[^0-9a-zA-Z]+", "_", v).strip("_").lower()`）不等价：`Huai-MAIN` → `basins_huai-main` ≠ `basins_huai_main`。
但这**不在本变更的因果路径上**：#1701 涉及的 `HYS_bst` 经 `.lower()` 已是正确 id。
且改成 `_slug_id` 也修不好真正的问题——只-改-根目录的 staging 约定使
`CJ-DTH-XJ` 目录名与注册 id `basins_dth_xj` 结构性不等，任何字符串规范化都补不上。
即"目录名 ≠ 注册 slug"是设计问题不是 patch。本 PR 只报不修。

### D4：`_named_basin_gis_dir()` 不动

`--basins` 显式路径解析走另一条函数，其 `child.upper()` 猜测对 `HYS_bst` 恰好命中 `HYS/BST`
（因为 D1 产出的 `parts[0]` 保留原始大小写）。不在 #1701 步骤 2 范围。

## 已知副作用

已提交的 `apps/frontend/public/geo/national-basin-{domain,river}.geojson` 是历史用
`--basins` 显式清单构建的（其 `basins_huai_main` 用 `_basin_id` 自动发现产不出），
本 PR 不重建产物、不改产物。产物与当前注册表的漂移（缺 #1699 七个新流域、仍含已退役
`basins_hhe`）是既存问题，另行报告。

## D5：spec delta 落在**新** capability，不挂 `basins-asset-discovery`

初稿把 delta 放进 `basins-asset-discovery`。交叉审查 + verifier 门判定改放新 capability
`national-geo-basin-discovery`，理由与代价核实如下：

- `openspec/specs/basins-asset-discovery/spec.md` 的五条既有 requirement 全部约束
  **registry discoverer**（`--basins-root` / `NHMS_BASINS_ROOT` CLI、errno/`ELOOP` 符号链检测、
  JSON inventory 字段表、`*.cfg.ic` fail-closed 注册、必需文件不可读时的状态降级），
  没有一条提到两份 geo builder。本变更自己的 proposal 就写明它们是「同一 Basins root 上的**第二套**发现实现」。
- 全仓 190 个 capability 里没有任何一个约束这两个脚本或 `national-basin-{domain,river}.geojson`
  （`grep -rn "build_national|national-basin|scripts/geo" openspec/specs/` 零命中）。
  所以正确处置不是「换一个已有 capability」，而是「本来就缺一个」。
- `openspec archive` 会把 delta **永久折进**目标 capability 的权威 spec
  （已用 `2026-08-10-symlink-loop-errno-detection` 的归档件与现行 spec 逐字节比对验证）。
  合并前迁移只是 fixture 内一次目录改名 + 重跑 `--strict`；合并并归档后再改，
  要动已折入的权威 spec、仍得新建 capability，且归档件永久错档。

保留说明：`basins-asset-discovery` 的 `## Purpose` 至今是
`TBD - created by archiving change m9-basins-model-assets`，所以「它不管这两个脚本」
**无法被证实也无法被证伪**——verifier 因此给 PLAUSIBLE 而非 CONFIRMED。
处置按「不确定时倾向修而非丢」，且该 delta 文件是本变更新引入的、不具备 defer 资格。
