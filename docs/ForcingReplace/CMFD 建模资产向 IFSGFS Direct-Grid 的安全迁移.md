# CMFD 建模资产向 IFS/GFS Direct-Grid 的安全迁移

关联文档：[ForcingGridReplace.md](./ForcingGridReplace.md)（standalone SHUD 手工工作流指南，非生产迁移规范）

> 规模口径说明：本文中 `13`、`13/13`、`11/13` 等数量来自 2026-07-06
> node-27 的**当时 13 模型历史审计基线**，只支撑对应审计结论，不是当前生产
> inventory。当前生产 authority 以
> [`current-production-ops.md`](../runbooks/current-production-ops.md) 为准：
> 2026-07-18 node-22 registry 恰为 18 个模型（12 旧 + 6 新）。执行任何迁移、
> cutover 或零影响断言时必须重新读取该当前 registry 的完整 model set；不得把
> 附录 A 的 13 模型样本机械扩写成 18 模型审计结果。

## 1\. 最终审查结论

文档提出的核心算法——读取既有三角网格、计算三角形代表点、匹配新的气象格点、重写 `.sp.att` 的 `FORC`——方向正确，正好补充了 SHUD-NWM 当前 direct-grid 设计未定义的“离线 GIS 模型资产迁移步骤”。

但文档目前不能直接作为生产迁移规范，必须修正以下关键问题：

| 原方案 | 最终决策 |
| --- | --- |
| 手工用 `seq()` 定义 GFS 网格 | 必须读取 SHUD-NWM canonical 产品的真实有序网格和 `grid_cell_id` |
| 依赖 `domain.shp` 行顺序对应 `.sp.att` | 禁止依赖行顺序，必须按显式 `element_id` 关联 |
| 默认 GFS/IFS 共用 0.25° binding | 共用与否由 `grid_signature` 等值判定（producer 同一算法实算，见 §2.2）；2026-07-06 历史基线实测 `ifs_0p25` 与 `gfs_0p25` 签名已一致，验证通过后应共用单一 binding，避免当时 13 模型各自产生双源几何相同的重复资产 |
| 离线写固定日期的 `.tsd.forc` 和气象 CSV | 迁移阶段不得生成周期性 forcing；这些文件由 SHUD-NWM producer 每个 cycle 动态生成 |
| 把 bbox 内所有格点写成 station | 只保留被至少一个三角形实际引用的 canonical cells |
| 用坐标字符串生成站点身份 | 站点身份必须绑定不可变 mapping asset，文件名是无语义的安全标识 |
| 只考虑文件回滚 | 生产层不存在回滚：必须解决 scheduler 路由、warm-state 兼容与 cycle-boundary 单向切换；切换后失败走 fix-forward（§11） |
| 宣称消除插值误差 | 只能宣称移除“IFS/GFS → CMFD 旧站点”的 IDW 层；仍存在 NWP 网格尺度和单元代表性误差 |

SHUD-NWM 现有代码已经实现 direct-grid contract、精确 `grid_cell_id` 取值、one-cell `weight=1.0` 映射、标准 SHUD forcing 包、运行时 `.sp.att FORC` 范围检查以及禁止 IDW fallback。迁移工作的正确定位是：**为这些运行期能力生产可信、不可变、可审计的 source-specific 模型资产**。

---

# 2\. 目标架构

## 2.1 资产分层

每个流域应拆分为四类逻辑资产：

| 资产  | 内容  | 生命周期 |
| --- | --- | --- |
| Hydrologic Core Asset | mesh、river、soil、geol、land cover、calibration、非 `FORC` 属性 | 长期不变 |
| Forcing Mapping Variant | source-specific `.sp.att FORC`、direct-grid binding、mapping metadata | 网格或算法变化时更新 |
| Dynamic Forcing Package | 每个 source/cycle 的 `.tsd.forc`、站点 CSV、lineage | 每周期生成 |
| Migration Evidence Package | ownership 表、diff、距离、QA、审批、回滚信息（资产级基线保留记录；生产激活层拒绝回滚，见 §11） | 与 mapping variant 同寿命且不可变 |

目标结构：

```text
Hydrologic Core（CMFD 或其他建模期驱动建成）
├── Legacy mapping variant（建模期驱动站点；不一定是 CMFD 格点，见附录 A）
├── GFS direct-grid mapping variant ─┐  grid_signature 验证一致时
└── IFS direct-grid mapping variant ─┘  合并为单一 shared binding（§2.2）
```

每个 mapping variant 应对应独立的 `model_input_package_id`，生产上建议对应独立 `model_id`，但共享相同 `basin_version_id`、river network 和 hydrologic-core fingerprint。

## 2.2 Source-specific 原则与共用判据

每个不同的规范化网格身份必须对应独立 mapping variant。**网格身份由 `grid_signature` 定义**（producer 同一算法：SHA-256 over 有序 `(grid_cell_id, lon@12dp, lat@12dp)`），**不由 `grid_id` 字符串定义**——`grid_id` 按 source 命名（`ifs_0p25` / `gfs_0p25`），跨 source 永不相等，不能作为共用判据（否则共用在逻辑上永远不可能达成）。

**现网实测（2026-07-06）**：`ifs_0p25` 与 `gfs_0p25` 归一化后 `grid_signature` 完全一致（`6c008901b8b7…`）。即两个 source 的 0.25° 网格在平台归一化（经度统一 -180..180、纬序、flatten）后就是同一个网格。

GFS 与 IFS 允许共用同一 binding，当且仅当：

1. registry 为该归一化网格登记了 source 无关的 canonical grid snapshot（含 `canonical_grid_key`），两个 source 的 `grid_id` 都映射到它；
2. 两个 source 的所有 required variables 在代表性 cycles 上均实算出与该 snapshot 相同的 `grid_signature`；
3. binding manifest 的 `applicable_source_ids` 显式包含两个 source（大小写按 §5.1 规范化，现网为 `IFS` 与 `gfs`）；
4. 共用验证证据（多 cycle、双 source 签名比对）归档在 evidence package。

不满足上述任一条 → 分别构建。若共用通过，2026-07-06 当时 13 模型历史
基线只需 13 份 binding 而非 26 份几何相同的重复资产；当前执行数量必须以
运行时 authority 的 model set 为准。

注意共用的只是 mapping binding：per-source 的 forcing package、scheduler 路由与 warm state（`hydro.state_snapshot` 唯一键含 `source_id`）仍然各自独立。当前 contract 是 manifest 级单一 `grid_id` 和 `grid_signature`，不支持 station 级 source variation。

## 2.3 禁止混合网格运行

一个 direct-grid forcing run 内，所有必需变量必须属于同一个规范化 source/grid identity。

当 IFS 不可用而切换 GFS 时，应：

1. 整个 run 切换为 GFS；
2. scheduler 选择 GFS direct-grid model variant；
3. 重新生产完整 GFS forcing package。

不得继续使用 IFS `.sp.att`，同时混入 GFS 格点值。除非两者已经通过完全相同 grid signature 的共用验证。

---

# 3\. 强制不变量

## INV-1：不原地修改

现有 CMFD model package、`.sp.att`、`.tsd.forc` 和历史 forcing version 必须保持不可变。

迁移始终发布新 package、新 mapping identity 和新 checksum。

## INV-2：只允许 forcing mapping 变化

Hydrologic Core 必须保持不变。

迁移后的模型包与 CMFD baseline 相比，仅允许：

- `.sp.att` 的 `FORC` 字段变化；
- 新增 direct-grid binding；
- 新增或更新模型资产 manifest/resource profile；
- 移除或隔离旧 CMFD weather forcing 文件；
- 因上述变化导致 package checksum 变化。

禁止变化：

- mesh/node/element topology；
- river/lake topology；
- soil、geology、land cover；
- calibration；
- `.sp.att` 非 `FORC` 字段；
- solver configuration；
- 初始状态向量结构。

## INV-3：Manifest 是权威，数据库是派生缓存

Direct-grid contract 必须来自不可变模型资产 manifest。`met.met_station` 和 `met.interp_weight` 只能是运行期派生缓存，不得反向成为 mapping authority。

当前 repository 从 `core.model_instance.resource_profile` 读取 contract，并且要求 authoritative nested contract；direct-grid station mirror 只以派生缓存身份写入数据库。

## INV-4：Explicit direct-grid 不得回退 IDW

一旦模型资产声明 `forcing_mapping_mode="direct_grid"`：

- contract 缺失必须失败；
- checksum 不一致必须失败；
- source scope 不匹配必须失败；
- grid drift 必须失败；
- cell 缺失必须失败；
- `.sp.att FORC` 越界必须失败；
- 标准 forcing package 缺失必须失败；
- 不得调用 legacy station loader；
- 不得计算 IDW；
- 不得把全部 `.sp.att FORC` 改写为 1。

当前 runtime 已按该原则要求标准多站 `shud/qhh.tsd.forc`，并在执行前验证 `.sp.att FORC` 是否属于 `.tsd.forc ID` 集合。

## INV-5：生产路由必须显式要求 direct-grid

SHUD-NWM 为兼容旧资产，在 contract 缺失时仍会走 IDW。因此，迁移后的生产 route 不能只依赖 producer 默认行为。

每个已迁移的 `basin + source + scenario` 必须登记：

```text
required_mapping_mode = direct_grid
required_model_id = <source-specific model variant>
required_grid_signature = <expected signature>
```

若实际选择的模型不满足上述约束，scheduler 必须在 forcing production 前失败，而不是静默使用 legacy IDW。

---

# 4\. Platform Readiness Gate

在迁移任何真实流域前，必须先通过平台级 Gate P0。

## P0.1 版本锁定

必须记录并锁定：

| 组件  | 必填身份 |
| --- | --- |
| SHUD-NWM | commit/tag |
| forcing producer | producer version |
| canonical converter | converter version |
| SHUD runtime | commit/tag |
| SHUD-OpenMP outer repository | commit/tag |
| SHUD solver submodule | exact commit |
| DB schema | migration version |
| PROJ/CRS database | version |
| mapping builder | algorithm version |

SHUD-OpenMP 本身通过 git submodule 引用 SHUD solver，因此不能只记录外层仓库版本；必须同时记录 submodule pin。 ([GitHub](https://github.com/SHUD-System/SHUD "GitHub - SHUD-System/SHUD: Simulator for Hydrologic Unstructured Domains (SHUD) --- multi-scale and multi-process integrated hydrological model · GitHub"))

> 这里是为了后续的openMP加速设计，当前NWM系统中SHUD只进行串行计算

## P0.2 实现证据

在固定 release 上必须通过：

1. direct-grid contract parser tests；
2. direct-grid producer tests；
3. exact-cell value tests；
4. standard SHUD package tests；
5. runtime staging tests；
6. out-of-range `.sp.att FORC` negative tests；
7. idempotency tests；
8. database migration tests；
9. 使用真实对象存储和真实数据库的 smoke test；
10. 使用生产 SHUD 二进制的最小流域执行。

仓库已有 compact E2E 验证 exact cell values、不调用 IDW、标准 station CSV、lineage、runtime staging 和 idempotency，但正式迁移仍须在实际部署 release 上重新执行。

OpenSpec task 状态与代码实现状态存在残余不同步，因此 readiness 不得仅依据 checkbox 判断，必须依据固定 commit、测试结果和部署 smoke evidence。

## P0.3 Solver forcing-consumer 审计

必须针对生产 SHUD solver pin 审计：

- `.sp.att FORC` 的所有读取者；
- 是否存在 river/lake 独立 forcing index；
- station `X/Y/Z` 是否参与数值转换；
- 是否存在高程订正；
- weather 之外的 `*.tsd.*` 输入；
- `Prcp_Correction`、LAI、MF 等辅助序列是否独立；
- legacy forcing 目录中哪些文件必须保留。

只有确认 `Z` 不参与数值计算后，才允许使用声明过的 sentinel。否则必须使用明确的 elevation source。

---

# 5\. Canonical Source-Grid Registry

## 5.1 Registry 内容

每个可用于 direct-grid 的网格必须注册为不可变 Grid Snapshot：

| 字段  | 要求  |
| --- | --- |
| `source_id` | 规范 source ID |
| `grid_id` | 与 canonical product 一致 |
| `grid_signature` | 使用 producer 同一算法计算 |
| `grid_definition_uri` | 不可变 URI |
| `grid_definition_checksum` | SHA-256 |
| `grid_cell_id` | 每格唯一 |
| longitude / latitude | 规范化后的 cell center |
| canonical ordinal | 有序位置 |
| longitude convention | 明确  |
| latitude order | 明确  |
| array flatten order | 明确  |
| native resolution | 明确  |
| valid-from / valid-to | 明确  |
| converter version | 明确  |

迁移工具不得自行重新实现一套“近似相同”的 signature 规则，必须调用或复用 producer 使用的同一 grid-signature 逻辑。

**落点与现状约束：**

- 数据库已有 `met.canonical_met_product` 表（含 `grid_definition_uri` 列）但现网 **0 行**；网格定义目前只以 `canonical/{source}/grid/{grid_id}/grid.json` 文件形式存在。Change 2 必须显式决策：复用该表还是新建 grid snapshot 表——不得两边各存一份互不校验。
- 下载 bbox 来自环境变量 `NHMS_DOWNLOAD_BBOX_*`（默认 63–145°E / 8–64°N）。`grid_cell_id` 是 bbox 裁剪后的**扁平索引字符串**（如 `"36268"`），**bbox 一变全体 cell id 漂移**。registry 必须把 bbox 作为 grid snapshot 身份的一部分钉死；生产部署 env 值与 registry 值不一致时 fail closed。2026-07-06 当时 13 模型历史基线（73–119°E / 28–43°N）均在默认覆盖内；这不构成对当前新增模型覆盖范围的验证。
- `source_id` 现网大小写不一致（`IFS` vs `gfs`）。registry 与 `applicable_source_ids` 校验必须定义统一的大小写规范化规则，且与 contract parser 的 normalize 逻辑同源。

## 5.2 稳定性验证

网格进入 registry 前，必须验证：

1. 多个 cycle 的签名一致；
2. 所有 required variables 的签名一致；
3. 下载 backend 改变时签名一致；
4. bbox clip 范围固定；
5. `grid_cell_id` 不随 cycle 变化；
6. NetCDF 纬度升降序不会改变 cell identity；
7. GFS `0..360` 和 IFS `-180..180` 按平台规则归一化；
8. product upgrade 后签名必须变化。

若 canonical 产品是按不同 bbox 动态裁剪且 cell IDs 会移动，则不得进入 direct-grid registry，必须先稳定 canonical grid contract。

---

# 6\. Mapping Algorithm Spec

## 6.1 算法标识

初始规范算法定义为：

```text
nearest_cell_barycenter_geodesic_v1
```

算法版本一旦发布，不得无版本号地改变距离定义、tie-break、索引顺序或坐标精度。

## 6.2 输入权威

几何与坐标权威：

1. `.sp.mesh` element/node 定义（元素三顶点 + 节点投影 X/Y）——唯一几何权威；
2. `.sp.att` 的 `INDEX` 列——element ID 权威；
3. model CRS 权威 = package 内 `gis/*.prj`（checksum-bound）。`.sp.mesh` 本身**不含任何 CRS 元数据**；2026-07-06 当时 13 模型历史基线是 12 套参数各异的自定义 Albers + 1 套 Transverse Mercator（qhh），全部 `PROJCS["unknown"]` 无 EPSG 码——CRS 必须逐包读取并写入 evidence，禁止任何全局 CRS 假设。

`domain.shp` **不作为算法输入**，仅用于可视化对比图。不得把 shapefile row number 视为 element ID。

必须验证：

- element ID 唯一、从 1 连续；
- `.sp.mesh` 与 `.sp.att` element ID 集合相等、element count 完全一致；
- package 内 `.prj` 存在、可解析、可与 WGS84 互转，其 checksum 记入 evidence；
- geometry 有效（三顶点非退化）；
- 行顺序变化不影响结果（按 ID 关联，不按行序）。

## 6.3 三角形代表点

每个 element 的代表点必须使用 authoritative mesh 三个顶点的几何重心：

```text
centroid = (vertex1 + vertex2 + vertex3) / 3
```

禁止使用可能经过简化或重排的展示图层坐标作为唯一依据。

## 6.4 最近格点规则

对每个 element centroid：

1. 转换至规范 WGS84 longitude/latitude；
2. 从 registered canonical grid cells 中寻找最小 geodesic distance 的 cell center；
3. 若多个 cell 在 tie tolerance 内距离相同，选择 canonical ordinal 最小者；
4. 记录距离、tie 状态和候选数；
5. 不得使用库默认但未声明的 planar-degree distance；
6. 不得通过经纬度字符串推断 `grid_cell_id`；
7. 对规则 lat/lon 网格，最近格心等价于对 lon、lat 独立就近取整；实现可用该等价加速，但结果必须与 geodesic 定义一致，tie 规则（第 3 条）不变。

该规则保持与原有最近站点/Voronoi ownership 语义相近，同时避免由局部投影选择引入跨流域差异。由于 `.sp.att` 每个 element 只能引用一个 forcing index，初始迁移不采用面积加权多格点方案。

## 6.5 Used-cell 子集

完成所有 element 映射后：

1. 取实际被引用的 unique `grid_cell_id`；
2. 删除未被任何 element 使用的 cells；
3. 每个 binding cell 必须至少被一个 element 引用；
4. 一个 `grid_cell_id` 只能对应一个 SHUD station；
5. used-cell 集合必须与 `.sp.att FORC` 所能引用的 binding 集合完全相等。

这可减少 station count、CSV 数量、数据库行数和 runtime IO。

**小流域最小 used-cell 规则（hard gate）**：used-cell 数 < 4 的流域**默认不迁移**。单格点驱动意味着全流域均匀 forcing、空间方差归零，比 legacy IDW（多邻居混合）信息量更少，是纯退化而非改进。现网实测 zhaochen_wem 全流域落在 1 个 0.25° 格点、zhaochen_mc 仅 4 格（附录 A）。此类流域只有两条路：留在 legacy mapping，或经 G11 科学特批（须证明寡格点驱动不劣于 control）。

## 6.6 `shud_forcing_index`

实际使用的 grid cells 按 canonical ordinal 排序后，连续分配：

```text
1, 2, ..., N
```

必须满足：

- 从 1 开始；
- 连续；
- 唯一；
- 可复现；
- 与 `.sp.att FORC` 一致；
- 与动态 `.tsd.forc ID` 一致；
- 与 binding station order 一致。

当前 contract parser 已要求 index 连续、station ID 唯一、filename 唯一。

## 6.7 `.sp.att` 更新

迁移必须：

1. 复制 baseline `.sp.att`；
2. 按 element ID 更新 `FORC`；
3. 保持行数、ID、schema 和所有非 `FORC` 数值不变；
4. 生成 parse-level semantic diff；
5. 生成 old/new checksum；
6. 禁止覆盖 baseline 文件。

验收必须证明：

```text
old_att[all columns except FORC]
==
new_att[all columns except FORC]
```

---

# 7\. Direct-Grid Binding Spec

## 7.1 Manifest 位置

当前 repository 路径要求 contract 位于 `core.model_instance.resource_profile` 的 nested section。最终规范统一使用：

```text
resource_profile.direct_grid_forcing
```

不得把 root-level `forcing_mapping_mode` 作为生产 contract 的唯一位置。

## 7.2 Manifest 必填字段

| 字段  | 要求  |
| --- | --- |
| `forcing_mapping_mode` | `direct_grid` |
| `binding_uri` | 不可变 object URI |
| `binding_checksum` | SHA-256 |
| `model_input_package_id` | 新 mapping variant package ID |
| `sp_att_path` | package 内明确路径 |
| `sp_att_checksum` | 新 `.sp.att` checksum |
| `applicable_source_ids` | 非空，严格 source scope |
| `grid_id` | 与 canonical product 相等 |
| `grid_signature` | 与 producer 实算相等 |
| `station_bindings` | 非空、连续、唯一 |

这些字段与当前 direct-grid contract parser 一致。

## 7.3 Station binding

每个 binding 必须包含：

| 字段  | 规则  |
| --- | --- |
| `station_id` | 全局唯一，包含 mapping asset identity |
| `shud_forcing_index` | 1-based 连续整数 |
| `forcing_filename` | 安全、无路径、case-fold 唯一 |
| `longitude` | 必须等于 canonical cell center |
| `latitude` | 必须等于 canonical cell center |
| `x` / `y` | cell center 转换到 model PCS |
| `z` | 按已批准 `z_policy` |
| `grid_id` | 等于 manifest grid ID |
| `grid_cell_id` | 存在于 registered grid snapshot |

额外强制检查：

- `grid_cell_id` 在 binding 内唯一；
- station lon/lat 与该 `grid_cell_id` 的 registered coordinate 一致——**“一致”按 grid_signature 同一规则（12 位小数舍入）比对后相等**，不是浮点字面相等（现网站点坐标带 ~1e-7° 浮点噪声，字面相等断言必然假失败）；
- 坐标基准显式声明：binding/registry 坐标为 WGS84；数据库 `met.met_station.geom` 为 SRID 4490（CGCS2000）镜像，两者数值差 < 1 m、对 0.25° 网格无判别影响，但所有相等性校验必须在同一基准内进行，禁止跨基准做相等断言；
- x/y 可从 lon/lat 和 model CRS 复算；
- filename 不得只依赖经纬度四舍五入；
- filename 不得与 `qhh.tsd.forc`、manifest、debug CSV 或模型输入文件冲突；
- 大小写不敏感文件系统上也不得冲突。

## 7.4 Station identity

`station_id` 必须包含不可变 mapping identity，避免新旧 binding 复用同一 station ID。

当前数据库 mirror 的 collision policy 会在相同 station ID 对应不同 binding checksum、model package 或 grid signature 时 fail closed。因此新 mapping version 不应复用旧版本 station IDs。

## 7.5 Z policy

允许的 policy 仅包括：

| Policy | 使用条件 |
| --- | --- |
| `canonical_orography` | canonical grid 提供可靠地形高程 |
| `model_dem_at_cell_center` | 已批准使用模型 DEM |
| `sentinel` | 已证明生产 SHUD solver 不使用 station Z 参与数值计算 |

Ownership 映射本身不需要 DEM；但在 solver audit 完成前，不得把“无需 DEM”扩展成对整个 binding contract 的无条件结论。

---

# 8\. 模型包与动态 Forcing 包边界

## 8.1 模型迁移阶段不得生成周期 forcing

Mapping builder 不得生成：

- 带 forecast cycle 起始日的 `.tsd.forc`；
- 每站气象时间序列 CSV；
- `met.interp_weight` 数据库行；
- `met.met_station` mirror；
- `met.forcing_version`；
- cycle-specific lineage。

这些均由 forcing producer 在运行期根据 canonical products 生成。

上传文档中“写入固定 startdate 的 `tsd.forc`”和“外部生成气象 CSV”可保留为 standalone SHUD 手工工作流说明，但不得成为 SHUD-NWM 生产迁移流程。

## 8.2 Direct-grid 模型包内容

Direct-grid model package 必须包含：

- hydrologic core 文件；
- 重写后的 `.sp.att`；
- direct-grid contract metadata；
- binding artifact 或其不可变 URI；
- 所有非天气辅助时间序列和配置；
- model core fingerprint；
- parent CMFD package identity。

Direct-grid 模型包不得包含可被误认为当前业务 forcing 的旧 CMFD weather station CSV。

如因兼容工具必须保留旧文件，应放在非运行目录并明确标记为 inactive，runtime 不得引用。

## 8.3 Dynamic forcing package

每个 cycle 由 producer 输出：

- standard `shud/qhh.tsd.forc`；
- 每个 used cell 一份 station CSV；
- debug/internal forcing；
- `forcing_package.json`；
- checksums；
- forcing lineage；
- station timeseries；
- pressure metadata/timeseries。

SHUD station CSV 只包含时间轴、Precip、Temp、RH、Wind、RN；Press 不写入 SHUD station CSV。

## 8.4 路径规范

Producer package 中 `.tsd.forc` 第 2 行必须使用 package-relative path，例如 `shud`。

禁止写入 build machine 的绝对目录。

SHUD 实际从 `.tsd.forc` 第 2 行读取 forcing 目录，而不是从 `cfg.para` 读取。

---

# 9\. Validation Gates

## Gate G0：Baseline Integrity

必须验证：

- baseline package checksum；
- `.sp.mesh` 可解析；
- `.sp.att` 可解析；
- element IDs 完整且唯一；
- 旧 `FORC` 均为正整数；
- 旧 `.tsd.forc` 若存在则引用合法；
- ancillary `*.tsd.*` 依赖清单完整；
- 当前 active model、state、source route 已登记；
- 站点坐标重复检查：同一 baseline 内坐标完全相同的多个 station 必须显式登记（现网 zhaochen_mc 有 4 站同点：X6–X9.csv，Z=-9999）；
- baseline 站点类型分类：不得假设“站点 = CMFD 0.1° 格点”——现网 zhaochen_wem 是 5 个不规则点（0.02° 间距、X1..X5.csv 命名、真实高程）；legacy mapping 的命名与处理必须覆盖非格网 baseline；
- startdate 异质性登记：2026-07-06 当时 13 模型历史基线的 `.tsd.forc` startdate 横跨 1951–2024，禁止任何“统一历史起点”假设；
- baseline `.tsd.forc` 第 2 行的构建机绝对路径（来自多个用户目录、含非 ASCII 路径）作为已知无害偏差归档，不修改 baseline（INV-1）。

失败时不得迁移，也不得顺手修复 baseline。

## Gate G1：Geometry Identity

必须验证：

- mesh、att element ID 集合一致（domain.shp 不参与算法，仅可视化，见 §6.2）；
- 三角形节点合法（非退化）；
- CRS 来自 package 内 `gis/*.prj` 且 checksum-bound（2026-07-06 当时 13 模型历史基线全部 `PROJCS["unknown"]` 无 EPSG 码，禁止全局 CRS 假设）；
- element count 一致；
- row reorder 不影响 mapping；
- mapping builder 使用 mesh ID 而非 row index。

## Gate G2：Grid Identity

必须验证：

- source/grid 已注册；
- grid signature 可使用 producer 同一逻辑复算；
- required variables 网格一致；
- representative cycles 网格一致；
- source scope 匹配；
- basin 完全位于 grid coverage 内；
- 不存在 silent dynamic crop。

## Gate G3：Ownership

必须验证：

- 每个 element exactly one cell；
- 每个 cell 存在于 registry；
- used-cell 与 station binding 一一对应；
- `FORC` 在 `1..N`；
- index 连续；
- duplicate grid-cell binding 为零；
- unused binding 为零；
- used-cell 数 ≥ 4（§6.5 小流域规则；不满足即 blocker，转 legacy 保留或 G11 特批）；
- tie 决策可复现。

必须输出距离指标：

- min；
- P50；
- P95；
- max；
- 按 cell size 归一化后的距离；
- tie count；
- coverage-edge count。

对于规则格网，若 element centroid 在有效格网覆盖内，最近中心距离不得超过局部 cell 半对角线加数值容差。超出通常意味着 CRS、裁剪范围或网格定义错误，应作为 blocker。

## Gate G4：Asset Delta

必须证明：

- core fingerprint 与 baseline 相等；
- mesh checksum 相等；
- river/lake 文件 checksum 相等；
- calibration checksum 相等；
- `.sp.att` 非 `FORC` 字段完全相等；
- 只新增 mapping metadata；
- 没有 legacy weather path 泄漏到 active package。

## Gate G5：Contract

必须验证：

- manifest 完整；
- binding artifact checksum 正确；
- manifest station bindings 与 binding artifact 一致；
- model input package identity 正确；
- `.sp.att` checksum 正确；
- source scope 正确；
- grid ID/signature 正确；
- station IDs 和 filenames 唯一；
- station coordinates 对应绑定 cell；
- x/y 可复算；
- z policy 有证据；
- reserved filename collision 为零。

## Gate G6：Producer

使用真实 canonical fixture 验证：

- explicit direct-grid 不调用 legacy station loader；
- 不调用 IDW neighbor search；
- 每站值等于 bound `grid_cell_id` 值；
- wind 仍由同一 cell 的 U/V 组合；
- canonical unit conversion 保持不变；
- required-cell 缺失时 fail closed；
- non-finite value 时 fail closed；
- rerun具有 idempotency；
- lineage 包含 binding、package、sp_att、grid 和 station identity。

## Gate G7：Runtime

必须验证：

- 标准多站 package 成功 staging；
- `.sp.att` 不被改写为单站；
- `.sp.att FORC` 全部属于 `.tsd.forc ID`；
- 缺 station CSV 时失败；
- CSV header 错误时失败；
- package checksum 错误时失败；
- mapping mode 由 checksum-verified package manifest 决定；
- model package 中旧 CMFD forcing 不可被 fallback 使用。

## Gate G8：Temporal Contract

SHUD forcing 时间序列采用 move-pointer 后的零阶保持读取，因此空间迁移不能忽略时间覆盖。

每个 dynamic forcing package 必须满足：

- 所有 stations 使用完全相同的 time lattice；
- Time_Day 单调严格递增；
- first value 覆盖 run start；
- final coverage 不早于 run end；
- 缺步和重复时间为零；
- source-specific f000/f003 规则由 producer 统一处理；
- `.tsd.forc` start date 与 CSV dates/Time_Day 一致；
- 禁止迁移工具写固定 `min(xfg$years)` 日期。

旧 CMFD forcing trim 的 2-day buffer 是特定 legacy 长序列裁剪策略，不应机械套用到 forecast direct-grid；forecast package应按 producer coverage contract验证。SHUD-OpenMP 的 forcing 说明也确认 CSV 时间列以天为单位，并由 solver 载入后零阶保持。

## Gate G9：容量

必须针对部署配置预估：

```text
DB timeseries rows
= station_count × timestep_count × output_variable_count
```

当前代码暴露的默认资源限制包括 10,000 stations、10,000 timesteps、10,000,000 timeseries rows 和约 32 MiB manifest；runtime 还对 direct-grid `.tsd.forc`、station CSV、`.sp.att` 字节数和行数设置边界。迁移前必须以实际部署配置进行容量检查。

容量证据必须包含现状基线对比：现网 legacy IDW 全网 6,290 站、`met.forcing_station_timeseries` 两周累计约 1.21 亿行（约 800 万行/天）；按站点位置估算迁移后 0.25° used-cell 全网约 1,200，预期约 5× 缩减。每个流域的迁移 evidence 必须给出迁移前后 station 数 / 时序行数 / 文件数的实测对比，而不是只贴公式——公式本身已在现网精确验证（附录 A：wem 5 站 × 56 步 × 6 变量 = 1,680 行与 DB 一致）。

超过限制时不得临时放宽并继续迁移，应单独提出性能/容量 change。

## Gate G10：State Compatibility

必须计算 `hydrologic_core_fingerprint`，至少覆盖：

- mesh topology；
- river/lake topology；
- `.sp.att` 非 `FORC` 字段；
- soil/geol/land；
- calibration；
- state vector schema；
- solver-relevant configuration。

该 fingerprint 不包含：

- `.sp.att FORC`；
- direct-grid binding；
- weather forcing package。

跨 mapping variant 复用初始状态仅在以下条件成立时允许：

1. core fingerprint 完全相等；
2. state schema 完全相等；
3. state checksum 有效；
4. snapshot valid time 与 run start 满足 runtime 时间一致性；
5. lineage 记录 cross-variant state transfer。

现网已确认 state 只能按同一 `model_id` 复用：`hydro.state_snapshot` 唯一键为 `(model_id, source_id, valid_time)` 且校验 `model_package_checksum`。因此必须在生产 cutover 前实现显式 state compatibility/clone 机制；否则 direct-grid 只能 cold-start 或 replay shadow，不能直接承担连续业务运行。

## Gate G11：Scientific Validation

Technical correctness 不等于 hydrological improvement。

Direct-grid 移除了旧 CMFD station IDW 层，但可能：

- 增大空间方差；
- 形成更明显的格点块状分布；
- 暴露 IFS/GFS native bias；
- 改变流量峰值和水量平衡；
- 与 CMFD calibration 产生新的系统偏差。

因此至少比较两条完全同条件路径：

| 路径  | Mapping |
| --- | --- |
| Current control | IFS/GFS canonical → IDW 到 CMFD-derived stations |
| Candidate | IFS/GFS canonical → exact grid-cell lookup |

比较必须使用：

- 同一 canonical products；
- 同一 solver build；
- 同一 initial state；
- 同一 run window；
- 同一 calibration；
- 同一输出间隔。

Forcing 比较应先展开到 element 级再按 element area 汇总，不得直接比较数量不同的 station 列表。

必须报告：

- precipitation total、wet-area fraction、P95/P99、spatial variance；
- temperature/RH/wind/RN distribution；
- element-level RMSE/correlation；
- outlet volume、peak、peak timing；
- water balance residual；
- groundwater/soil moisture/river stage extrema；
- solver stability；
- 与观测的 NSE/KGE/bias，如有可靠观测。

全局不预设一个统一水文阈值。Pilot 阶段必须建立 basin-class acceptance envelopes，后续 batch 使用经审批的阈值 registry。

---

# 10\. 发布和切换流程

## Phase 0：平台冻结

完成 P0 readiness，锁定所有软件、schema 和 solver 身份。

## Phase 1：全国资产盘点

每个现有和后续 CMFD 流域必须登记：

- core package；
- current model ID；
- current forcing stations；
- `.sp.att` checksum；
- active source routes；
- active states；
- ancillary time-series dependencies；
- migration eligibility；
- legacy baseline identity（calibration/replay 保留基线；不作生产回滚 target——生产层拒绝回滚，见 §11）。

## Phase 2：Grid Registry

分别建立 GFS 和 IFS grid snapshots。

在未证明 signature 完全一致前，禁止共用 binding。

## Phase 3：Pilot Build

选择至少三类流域（现网候选见附录 A）：

- 小流域：keliya（484 elements / 约 8 个 0.25° used cells）；
- 大流域：heihe（6,335 elements / 约 312 cells）；
- 边界复杂、跨大量 NWP cells：zhaochen_hhy（7,800 elements / 约 260 cells）。

zhaochen_wem（1 cell）与 zhaochen_mc（4 cells）列入“暂不迁移”，走 §6.5 小流域规则。

每个流域先只迁移一个 source，避免同时引入双 source 路由风险。

## Phase 4：Technical Shadow

生成 source-specific model variant，完成 G0–G10。

此阶段不进入外部生产发布。

## Phase 5：Scientific Shadow

覆盖：

- 干旱期；
- 常规降水；
- 强降水；
- 多个季节；
- 雪冻流域则必须包含冻融期。

输出 control 与 candidate 的 forcing 和 hydrological comparison。

## Phase 6：State/Cutover Rehearsal

必须演练：

1. legacy → direct state transfer（与 activation 同一 DB 事务的指纹门控 clone）；
2. direct forcing production；
3. runtime execution；
4. fix-forward：direct → direct′（重建 variant 后 direct-grid 间换活；不存在 direct → legacy 回滚）；
5. fix-forward 后 state continuity（M1→M1′ 指纹门控 clone）；
6. forcing package 必须按目标 model variant 重新生成。

## Phase 7：Canary

只激活少量流域和少量 cycle。

Canary route 必须显式 pin：

- source；
- direct-grid model ID；
- model input package ID；
- grid signature；
- mapping mode；
- solver version。

## Phase 8：Batch Rollout

每批必须包含：

- batch ID；
- basin/source/model variant 清单；
- validation evidence；
- scientific approval；
- activation window；
- fix-forward 预案（不含 legacy 回滚 target）；
- state transition plan；
- on-call owner。

禁止一次性全国切换。

## 切换可见性边界（node-22 计算 / node-27 展示）

“无感迁移”必须按组件拆开回答，不许笼统承诺：

**node-22 计算侧：可以无感。**
runtime 的标准多站 staging 是双模式共用路径（direct-grid 只是叠加 fail-closed 校验）；Slurm/sbatch、SHUD 二进制、producer 产物布局（`shud/qhh.tsd.forc` + 每站 CSV）全部不变。唯一前提：scheduler 按 Change 4 路由到新 model variant。

**node-27 数据/展示侧：不是自动无感，有三个具体断点，逐个处理：**

1. **站点混显（shadow 期硬 bug）**：station-MVT 图层按 `met.met_station WHERE basin_version_id=… AND active_flag=true` 查询（`apps/api/routes/hydro_display.py`），**不按 model_id 过滤**。mapping variant 共享同一 `basin_version_id`，双轨期间 direct-grid station mirror 一落库，图层即新旧两套站混显（并可能触发 MVT feature budget 上限）。处置（已定案）：保持单轨——shadow 期新站 mirror 一律 `active_flag=false`（由 Change 4 注册步骤写入；§8.1 禁 mapping 阶段写 met 表）；cutover 在与 model activation 同一事务内原子翻转两套站的 active_flag，MVT 查询本身不改。切换后 retention 窗口内浏览旧 cycle 时，新针脚弹窗命中现成 retention 空态（`adapt-cycle-picker-retention-window` 能力），失配随窗口自然消失；station 单查端点补 active_flag 过滤，收口 evidence-only 行元数据可枚举问题。
2. **气象代站时序端点的 model_id 参数**：`read_station_forcing_csv` 经 station inventory 的 `forcing_filename` 解析 CSV，**不依赖 `X<lon>Y<lat>` 文件名模式**，新命名规则天然兼容；但存储路径含 `model_id` 段——cutover 后 model_id 变化，display/前端必须动态解析 active model，不得 pin 旧 model_id。历史 cycle 按旧 model_id 查询仍然有效（旧资产不可变）。
3. **流量展示的水文连续性**：river network 与 hydrologic core 不变（INV-2），流量图层结构上无感；但没有 Change 5 的 state clone，direct-grid 首轮只能冷启动，spin-up 期流量曲线会出现肉眼可见的失真/断裂。**cycle-boundary cutover + 兼容 state 转移是流量展示无感的硬前提**，不是可选优化。处置（已定案）：Change 5 的指纹门控 state clone 与 activation 同一 DB 事务执行，杜绝「已激活未转移」中间态。

**必须接受的可见变化（这是迁移目的本身，不是回归）**：气象代站图层站点数约 5× 减少、位置从 legacy 站点变为 0.25° 格心、station_id 与文件名全部更换；跨 cutover 的“同站连续时间序列”不存在，展示层如有跨期拼接逻辑必须按 variant 分段。

---

# 11\. 失败处置 Spec（单向通道：拒绝回滚）

**决策：direct-grid 是单向通道。一个 basin 一旦激活 direct-grid variant，永不回滚到 legacy-mapping（IDW）模型；lifecycle 层硬 guard 拒绝任何 legacy 重新激活请求（fail-closed，无 override）。应急路径只有 fix-forward。**

## 11.1 触发条件

以下任一情况必须暂停激活（切换前）或触发 fix-forward（切换后）：

- grid signature drift；
- contract/checksum mismatch；
- `.sp.att FORC` 越界；
- standard SHUD package缺失；
- scheduler 选择错误 mapping mode；
- mixed-grid canonical products；
- station mirror collision；
- 容量越限；
- solver crash 或 NaN；
- water balance 异常；
- 未批准的 hydrological degradation；
- warm-state 不兼容。

## 11.2 处置方式

**切换前**（shadow/canary 期，legacy 仍 active）：不激活即可，无需任何回滚动作；修复或重建 direct-grid 资产后重新走验证门。

**切换后**：fix-forward，必须在 cycle boundary 执行：

1. 重建 direct-grid variant（新 release 包 + 新 `model_instance`，M1→M1′）；
2. 指纹门控通过后在 direct-grid variant 之间换活（lifecycle 允许 direct→direct′）；
3. 使用新 variant 重新生产 forcing；
4. 修复期间该 basin+source 可暂停出品，display 按 best-available 展示既有 cycle；
5. 保留失败 direct-grid package和证据；
6. 不修改失败资产；
7. 不通过 producer 内部 fallback 实现任何降级；
8. 禁止重新激活 legacy-mapping 模型（硬 guard）。

## 11.3 State 处置（fix-forward 下）

优先顺序：

1. M1→M1′ 指纹门控 clone（fix 只动 FORC/binding 时指纹必然相等）；
2. 指纹不相等（hydrologic core 变了）说明这不是 fix-forward 而是新模型：显式冷启动 + 审批；
3. 冷启动仅作为明确降级策略，spin-up 期流量失真必须公告。

---

# 12\. 后续新流域建模规范：direct-grid 为默认生产 mapping

后续仍可使用 CMFD（或其他历史数据集）建模和率定——率定需要长历史序列，这是 legacy 驱动唯一的保留理由。但**新流域的业务预报路由从第一天起就是 direct-grid**，不得先上 IDW 运营路径再“择期迁移”：

- 新流域没有既有业务连续性、没有 warm state 迁移问题、没有旧站点展示历史——存量迁移的大部分风险控制（vs-IDW shadow A/B、state clone、cutover 演练）对它们不适用；
- 给新流域先铺 IDW 运营路径等于主动制造下一批待迁移存量。

建模发布必须产出：

| 产物  | 必须  |
| --- | --- |
| Hydrologic Core Asset | 是   |
| Legacy mapping asset（率定期驱动映射，仅作率定/历史 replay，**不注册业务路由**） | 是   |
| GFS direct-grid mapping variant | 是，若 GFS 为业务 source |
| IFS direct-grid mapping variant | 是，若 IFS 为业务 source（与 GFS 签名一致时共用 binding，§2.2） |
| Grid snapshot references | 是   |
| Mapping evidence package | 是   |
| Core fingerprint | 是   |
| State compatibility metadata | 是   |
| Scientific validation status | 是   |

默认状态：

```text
Legacy mapping    = calibration / historical replay only（不进业务路由）
GFS direct        = 默认业务 mapping（G0–G9 技术门 + 率定期 replay 验证通过即 production-ready）
IFS direct        = 同上（与 GFS 签名验证一致时共用同一 binding，见 §2.2）
```

新流域的科学验证（G11）相应简化：direct-grid 率定期 replay 与率定结果的一致性 + 预报期 forcing 合理性检查即可；**不要求** vs-IDW 的运营 A/B——没有需要保护的旧运营基线。

例外：used-cell 数 < 4 的小流域（§6.5）——direct-grid 单/寡格点驱动对其是退化。允许二选一并记录在案：按 legacy 站点映射方式注册业务路由，或经科学特批后仍用 direct-grid。

2026-07-06 当时 13 模型历史基线中的存量模型仍按 Phase 0–8 走完整迁移
流程；本节默认规则只适用于新增流域。实际迁移批次必须从当前 registry 重新取数。

---

# 13\. Grid Drift 生命周期

任何以下变化都必须视为新 grid version：

- cell count 变化；
- 坐标变化；
- 纬度顺序变化；
- longitude convention 变化；
- `grid_cell_id` 变化；
- flatten order 变化；
- bbox/subset 变化；
- converter 重定义 cell identity；
- source 产品升级。

处理流程：

1. 旧 binding 自动失效；
2. forcing production fail closed；
3. 注册新 grid snapshot；
4. 构建新 mapping variant；
5. 完成 shadow/canary；
6. 激活新 variant；
7. 保留旧 variant 供历史复现。

禁止只修改 manifest 中的 `grid_signature` 而不重建 `.sp.att` 和 station bindings。

---

# 14\. Evidence Package

每个 mapping variant 必须包含或引用：

| Evidence | 内容  |
| --- | --- |
| baseline identity | CMFD package、att、mesh checksums |
| grid snapshot | ordered cells、signature、checksum |
| ownership table | element ID、old FORC、new FORC、cell ID、distance |
| station binding | 完整 contract rows |
| asset diff | 所有文件及字段变化 |
| distance QA | min/P50/P95/max/ties |
| map evidence | old/new ownership |
| contract validation | 所有 hard gates |
| capacity report | station/time/row/file size |
| producer evidence | exact lookup、no IDW |
| runtime evidence | staging、FORC range |
| state compatibility | core fingerprint、state transition |
| scientific report | forcing/hydrology A/B |
| approvals | builder、reviewer、scientific approver |
| rollback record | target model/state（资产级；生产层无回滚，§11） |

Evidence package 必须不可变，并与 mapping asset checksum 互相绑定。

---

# 15\. 建议的 OpenSpec Change 拆分

## Change 1：`cmfd-direct-grid-platform-readiness`

覆盖：

- release pin；
- DB migrations；
- production E2E；
- solver forcing-consumer audit；
- resource limits；
- ancillary forcing inventory。

## Change 2：`canonical-source-grid-registry`

覆盖：

- ordered grid snapshot；
- grid signature；
- cross-cycle stability；
- variable-grid consistency；
- GFS/IFS sharing rules（§2.2 signature 判据 + source 无关 `canonical_grid_key`）；
- `met.canonical_met_product` 复用 or 新表的显式决策（现表 0 行、含 `grid_definition_uri` 列）；
- bbox 钉死（`NHMS_DOWNLOAD_BBOX_*` 与 registry 不一致时 fail closed）；
- `source_id` 大小写规范化（现网 `IFS` vs `gfs`）；
- grid drift lifecycle。

## Change 3：`forcing-mapping-asset-build`

覆盖：

- mesh/att ID association；
- barycenter nearest-cell algorithm；
- tie-break；
- used-cell subset；
- `FORC` rewrite；
- binding generation；
- immutable package和evidence。

## Change 4：`source-specific-model-variant-routing`

覆盖：

- direct-grid variant 注册：新 `core.model_instance` 行（新 model_id），粒度 basin × `canonical_grid_key`，IFS/GFS 签名相同时共用一个 variant（`applicable_source_ids=[gfs, IFS]`），legacy 行保留作 calibration/replay；
- variant 注册同步写入 `met.met_station` cell 站 mirror 行（初始 `active_flag=false`）——§8.1 禁 mapping 阶段写 met 表，镜像行归注册步骤；
- 切换开关复用现有 model activation 生命周期（单事务换活/审计/幂等/并发安全），不新建 per-(basin, source) 路由表；activation → scheduler registry manifest 重发 → NFS → node-22 被动消费；
- 源级隔离：dispatch/staging 层按 `applicable_source_ids` fail-closed（源不在列表 → 拒跑，INV-4 延伸）；
- 计算层零跨源替代：一轮 run 全程单源、禁 mid-run 拼接、禁 legacy 包兼容层；某源某 cycle 缺数据 = 该源该轮无 run（记录原因），跨源可用性仍由 display best-available-selection 负责；
- lifecycle 硬 guard：basin 有 direct-grid 激活史后，activate(legacy-mapping) 一律 REFUSE；direct→direct′ fix-forward 换活允许（§11）；
- 旧 variant 下线语义（显式 spec 化）：M1 激活即 M0 同事务 superseded 并退出 dispatch 候选——调度消费面本就按 `lifecycle_state=='active'` 过滤（`scheduler_file_providers.py`），此行为升级为 spec requirement + 回归测试锁定；配合硬 guard 构成**永久下线**，杜绝不同格点来回切换引入的误差；M0 保留为不可变谱系（历史产品/指纹门 clone 来源/离线 calibration-replay），下线不等于销毁；
- cutover 事务 owner：lifecycle op 定义显式扩展点，供 Change 5（state clone）与 Change 8（station flag 翻转）按序挂载。

## Change 4.5：`direct-grid-build-enablement`

覆盖（pilot 前置 enablement，三项无主项收口）：

- z_policy 权威定案：在 pinned solver 源码（submodule `3aec6575`）与生产 `.cfg` 高程订正开关状态上做窄幅审计，产出书面 verdict（∈ {sentinel, model_dem_at_cell_center, canonical_orography}）并修正 `direct-grid-binding-artifact` spec 的悬空指向（原 solver audit 已随 readiness #895 descope）；
- `workers/mapping_builder/cli.py` 操作入口（proposal/design 已点名、实现缺席的 drift 收口，builder 目前 library-only）；
- `verify_download_bbox_matches_registry` 接入 producer preflight（registry Task 3.2 non-goal 移交项）：部署 bbox 与 registry 不一致时运行时 fail-closed，含经度约定（0..360 vs -180..180）责任落点。

## Change 5：`mapping-variant-state-compatibility`

覆盖：

- 指纹门控 state clone：cutover 时把 (M0, source, t*) 最新合格 snapshot 克隆为 (M1, source, t*)，物理 state 文件不复制（mesh 同一，天然合法），`model_package_checksum` 盖 M1 包并记 `cloned_from` 谱系；前置门 `hydrologic_core_fingerprint` 相等，不等则拒绝（fail-closed，退化为显式冷启动审批）；
- clone 与 activation 同一 DB 事务（挂 Change 4 lifecycle 扩展点），任一步失败整体回滚，无「已激活未转移」中间态；
- 拒绝回滚的 state 侧配合：无反向 clone；fix-forward M1→M1′ 时走同一指纹门 clone（§11.3）；
- replay 策略：谱系钉源——cutover 前区间 replay/calibration 用 M0 离线跑（非激活操作，硬 guard 不拦），cutover 后区间用 M1。

## Change 6：`direct-grid-scientific-validation`

覆盖：

- element-level forcing comparison；
- hydrological shadow；
- basin-class thresholds；
- observation comparison；
- approval workflow。

## Change 7：`direct-grid-batch-rollout`

覆盖：

- pilot；
- canary；
- batch；
- monitoring；
- fix-forward drill（direct→direct′ 换活演练，无 legacy 回滚）；
- future basin direct-grid-by-default workflow（§12）。

## Change 8：`direct-grid-display-cutover`

覆盖：

- 单轨翻转：cutover 事务内原子翻 `met.met_station.active_flag`（M0 站全 false、M1 cell 站全 true，挂 Change 4 lifecycle 扩展点）；station-MVT 查询本身不改，双轨混显由「shadow 期 mirror 恒 false + 单事务翻转」根除；
- 旧 cycle 时态：切换后 retention 窗口内浏览旧 cycle，新针脚弹窗渲染现成 retention 空态（非报错），失配随窗口自然消失；「历史 cycle 按旧 variant 查询」收窄为产品/时序数据层面（按 cycle+model 键，不受 flag 影响）的回归验证；
- display / 前端 active model_id 动态解析（spec requirement：任何 surface 禁止跨 cutover pin/缓存 model_id）；
- station 单查端点 active_flag 过滤收口（readiness §2.4 N1 记录的 evidence-only 行元数据可枚举问题）；
- node-27 live receipt：在 readiness 合成身份上跑真实事务彩排 cutover（flag 翻转前后 MVT diff + 弹窗 retention 空态实机截证 + 2026-07-06 当时 13 模型历史基线的零影响 SQL 断言）；实际 cutover 还必须对当前 registry 完整 model set 重跑零影响断言。流量曲线跨 cutover 连续性 receipt 显式 DEFER 至 pilot 首次真 cutover 补录（缺席写入证据记录，不算认证缺口）。

---

# 16\. 最终 Go/No-Go 标准

一个 `basin + source` 只有满足以下全部条件才能激活 direct-grid：

1. legacy baseline immutable（CMFD 或其他建模期驱动）；
2. 新 model variant 已发布；
3. core fingerprint 与 baseline 相等；
4. `.sp.att` 仅 `FORC` 变化；
5. element ID 关联无缺失；
6. canonical grid snapshot 稳定（含 bbox 钉死，§5.1）；
7. source-specific grid signature 匹配；
8. GFS/IFS 共用严格按 §2.2 signature 判据执行（未验证不得共用；验证通过不得重复建双份）；
9. station bindings 完整；
10. used cells 与 binding cells 完全相等；
11. used-cell 数 ≥ 4，或已获 G11 小流域特批（§6.5）；
12. station coordinates 与 grid cell 按 §7.3 容差规则一致；
13. `FORC` 连续且范围合法；
14. contract checksum 全部通过；
15. producer exact lookup 通过；
16. no-IDW 证据通过；
17. runtime 标准多站 staging 通过；
18. legacy fallback 被拒绝；
19. temporal coverage 通过；
20. 容量限制通过（含 G9 现状基线对比）；
21. ancillary forcing 未受影响；
22. state compatibility 通过；
23. hydrological shadow 通过；
24. canary 通过；
25. fix-forward 演练通过（direct-grid 间换活；无 legacy 回滚路径，§11）；
26. route 显式要求 `direct_grid`；
27. display 侧站点集合按 variant 原子切换、无混显，气象代站时序与流量展示跨 cutover 验证通过（Change 8）；
28. evidence package 归档并审批。

**最终实施顺序应是：先平台 readiness，再 GFS/IFS grid registry，再落地 route/state/display 切换机制（Change 4/4.5/5/8，全部只落机制、不激活任何生产 basin），再单一 source pilot（shadow，依赖 Change 4 的注册与 Change 4.5 的构建工具链），最后经 canary 按批激活迁移。不得先批量重写** `.sp.att`**；激活永远发生在 pilot/canary 验证之后。**

---

# 附录 A：当时 13 模型历史基线核查（2026-07-06，node-27 实测）

本规范多处规则的历史审计依据。数据来自 2026-07-06 node-27 primary PG 与
NFS 上当时 13 模型的模型包实测；不代表 2026-07-18 当前 18 模型 inventory。

| basin | elements | stations | FORC 实际引用 | est. 0.25° used cells | 投影 | tsd.forc startdate |
| --- | --- | --- | --- | --- | --- | --- |
| heihe | 6,335 | 1,709 | 1,573 | ~312 | Albers | 19510101 |
| hetianhe | 4,872 | 581 | 494 | ~120 | Albers | 19900101 |
| kashigeer | 3,204 | 941 | 846 | ~174 | Albers | 19510101 |
| keliya | 484 | 32 | 28 | ~8 | Albers | 19510101 |
| qhh | 4,773 | 386 | 347 | ~75 | **Transverse Mercator** | 19790101 |
| qinyijiang | 3,155 | 93 | 80 | ~20 | Albers | 19550101 |
| tailanhe | 1,614 | 37 | 28 | ~11 | Albers | 19510101 |
| weiganhe | 3,158 | 401 | 361 | ~77 | Albers | 19510101 |
| xinanjiang_upstream | 801 | 50 | 37 | ~12 | Albers | 19580101 |
| zhaochen_bst | 4,732 | 626 | 537 | ~120 | Albers | 19510101 |
| zhaochen_hhy | 7,800 | 1,420 | 1,303 | ~260 | Albers | 19510101 |
| zhaochen_mc | 1,900 | 9 | 9 | **4** | Albers | 20240701 |
| zhaochen_wem | 967 | 5 | 5 | **1** | Albers | 20220611 |

核查要点：

- 当时 13 个 model 实例全部**无 direct-grid contract**，全在 legacy IDW 路径（IFS + GFS 双 source，每站 4 邻居 × 6 变量）；mapping builder 代码中不存在（仅有 contract parser / producer consumer / runtime 校验）。
- **当时 13 模型历史基线的模型输入权威源 = object-store release-frozen 包**（`/home/ghdc/nwm/object-store/models/basins_<basin>_shud/<release>/package/`，当时 13/13 basin 全部齐全，33 个 release 覆盖 13 buckets），**非** `/home/ghdc/nwm/Basins/<basin>/input/<basin>/`（node-27 建模者 dev workspace，best-effort，当时 6/13 齐全 — qhh/keliya/heihe/zhaochen_{bst,wem,mc}；其余 7 个 hetianhe/kashigeer/weiganhe/xinanjiang_upstream/tailanhe/qinyijiang/zhaochen_hhy 只有 CALIB/forcing/input 骨架而无 sp.att/tsd.forc，其完整 SHUD 输入唯一存放在 object-store release 中）。node-22 上 `/volume/nwm/Basins/` 是 dev-workspace 副本，与 node-27 一致，同样不是权威。mapping builder（Epic #909）读取源必须锁定在 object-store 上，不得读 dev workspace。
- `ifs_0p25` 与 `gfs_0p25` 的 `grid_signature` 实测一致（`6c008901b8b7…`），而 `grid_id` 字符串不同——§2.2 共用判据的直接依据。
- `grid_cell_id` 为 bbox 裁剪后的扁平索引字符串（如 `"36268"`）；bbox 来自 env `NHMS_DOWNLOAD_BBOX_*`（默认 63–145°E / 8–64°N），当时 13 模型历史基线（73–119°E / 28–43°N）均在覆盖内——§5.1 bbox 钉死的依据。
- 当时 11/13 流域 baseline 存在 unused station——§6.5 used-cell 子集规则的现实依据。
- zhaochen_wem：5 个不规则站点（0.02° 间距、`X1..X5.csv`、真实高程），**非 CMFD 格点**；zhaochen_mc：4 站坐标完全重复（X6–X9 同点，Z=-9999）——G0 站点分类与重复检查的依据。
- 所有投影均 `PROJCS["unknown"]` 无 EPSG 码，CRS 只能取自 package 内 `gis/*.prj`——§6.2 CRS 权威规则的依据。
- 数据库 `met.met_station.geom` 为 SRID 4490（CGCS2000），binding/registry 坐标为 WGS84，现网站点坐标带 ~1e-7° 浮点噪声——§7.3 容差规则的依据。
- 容量公式实测验证：zhaochen_wem 单个 forcing version = 5 站 × 56 步 × 6 变量 = 1,680 行，与 DB 精确一致；`met.forcing_station_timeseries` 两周累计约 1.21 亿行（约 800 万行/天）——G9 的依据。
- `hydro.state_snapshot` 唯一键 `(model_id, source_id, valid_time)` 且校验 `model_package_checksum`：跨 model_id 无 warm state 可用——G10 / Change 5 的依据。
- `met.canonical_met_product` 表存在（含 `grid_definition_uri` 列）但 0 行；网格定义现仅存 `canonical/{source}/grid/{grid_id}/grid.json`——§5.1 registry 落点决策的依据。
- station-MVT 图层查询按 `basin_version_id + active_flag`、不按 model_id（`apps/api/routes/hydro_display.py`）；气象代站时序端点（`packages/common/object_store_forcing.py` 的 `read_station_forcing_csv`）经 station inventory 的 `forcing_filename` 解析 CSV，不依赖 `X<lon>Y<lat>` 文件名模式——§10 切换可见性边界与 Change 8 的依据。
- baseline `.tsd.forc` 第 2 行含 5 个不同构建机用户目录的绝对路径（含非 ASCII 路径）；producer 生成的周期包第 2 行已是 package-relative `shud`，runtime staging 时重写为实际 staging 目录——§8.4 与 G0 归档项的依据。

（est. used cells 按现有站点位置向 0.25° 格心归并估算；正式数字以 element 质心映射为准。）
