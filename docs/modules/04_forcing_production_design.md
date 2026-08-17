# 04. SHUD forcing 生产模块：模块设计

版本：v0.2  
日期：2026-05-06

## 1. 模块目标

把 canonical 气象产品提取到 SHUD 气象代站，并生成 `.tsd.forc` 和 forcing CSV。

## 2. 上下游关系

| 方向 | 内容 |
|---|---|
| 上游 | Canonical Meteorological Product、Model Registry、预计算空间权重。 |
| 下游 | SHUD Analysis/Forecast Run。 |
| 主要数据表/存储 | `met.forcing_version`, `met.met_station`, `met.forcing_station_timeseries`, `met.interp_weight` |

## 3. 职责边界

- 读取模型气象代站定义。
- 按模型/input 资产的 `forcing_mapping_mode` 选择空间映射：缺省或显式 `idw` 使用 legacy IDW；显式 `direct_grid` 使用模型资产预计算 binding。
- 上一条描述的是 producer 的历史兼容能力；当前生产 scheduler 设置
  `NHMS_SCHEDULER_REQUIRE_DIRECT_GRID=true`，registry 发布与加载都拒绝缺省/`idw` 行。
  新增流域必须先生成 GFS、IFS 两个 source-scoped direct-grid variant 才能进入业务运行。
- 生成 PRCP、TEMP、RH、wind、Rn、Press。
- 输出 SHUD forcing 文件包。
- 写入 forcing_station_timeseries。

## 4. 不负责事项

- 不承担其它模块的数据修复职责。
- 不绕过统一状态机直接发布产品。
- 不覆盖历史版本；任何变更通过新版本或状态切换表达。
- 不在日志中写入密钥、账号、token 等敏感信息。

## 5. 关键设计

### 5.1 Manifest / metadata first

模块输入输出必须先有元数据登记，再有实际文件或时序数据写入。这样可支持失败恢复、重跑和结果追溯。

### 5.2 幂等运行

同一任务重复执行时，如果目标产物已经存在并通过 checksum/QC，模块应返回 already_done，而不是重复写入或覆盖。

### 5.3 质量标识

模块输出必须包含 `quality_flag` 或同等字段。严重质量问题阻断发布；可接受问题保留标记供前端和分析人员识别。

### 5.4 双模式空间映射契约

`forcing_mapping_mode` 是模型/input 资产级契约，不是全局运行配置：

- 缺省或显式 `idw`：保持 legacy 行为，读取 `met.met_station` 中的固定 SHUD forcing station，按 canonical 网格计算或复用 IDW 权重，写入标准 SHUD 包。
- 显式 `direct_grid`：只接受模型资产 manifest 中的 direct-grid binding。每个 station 必须有
  `station_id`、`shud_forcing_index`、`forcing_filename`、`grid_id`、`grid_cell_id` 和坐标；
  producer 校验 `applicable_source_ids`、binding checksum、模型 input package identity、
  `.sp.att` path/checksum、canonical `grid_id` 与 `grid_signature` 后，按 `grid_cell_id` 精确取值。

direct-grid 失败必须 fail closed：binding 缺失、source 不在 `applicable_source_ids`、grid signature 漂移、`.sp.att FORC` 引用不在 `.tsd.forc ID` 范围内，或模型 input identity 不匹配时，不得回退 IDW，也不得发布 ready forcing version。

### 5.5 输出与 lineage

两种模式都输出标准 SHUD 包：主站点索引成员 `shud/stations.tsd.forc` 与每站 CSV。站点 CSV 只包含 `Time_Day/Precip/Temp/RH/Wind/RN`；`Press` 可保留在 `met.forcing_station_timeseries`、package manifest `units` 或 lineage 元数据中，但不得写入 SHUD CSV。

#### 5.5.1 主站点索引成员身份迁移（#1176）

主站点索引成员名是**流域中性的纯运输身份**，不承载任何流域证据：

- **canonical**：`shud/stations.tsd.forc`（manifest `role = shud_forcing`）。producer 只产此名，所有流域一致。
- **legacy**：`shud/qhh.tsd.forc`。历史 object-store package 不重写，runtime/消费方**只读接受**。
- **恰一决断（fail-closed 两翼）**：direct-grid 消费方在 package manifest 与解包后的文件系统两层各自要求
  `{canonical, legacy}` 中**恰好一个**成员。两者并存 → `DIRECT_GRID_FORCING_INDEX_AMBIGUOUS`；
  零个（direct-grid）→ `DIRECT_GRID_STANDARD_SHUD_FORCING_MISSING`，报错文本列出两个可接受身份；
  manifest 声明的成员在对象树中缺席 → `FORCING_CHECKSUM_READ_FAILED` 点名声明成员（不是 size-limit 码）。
  单一事实源为 `packages/common/shud_forcing_contract.py`。
- **direct-grid staging 卫生（#1355）**：`input/<project>` 在同一 run_id 的各次 attempt 间复用，故 direct-grid
  staging 写入前先删除本次 checksum 校验后的 package manifest **未声明**的可接受索引成员——跨 #1176 身份迁移
  重跑时，上一 attempt 残留的另一身份就此自愈，不再让文件系统层的并存把该 run_id 永久卡死。删除失败以
  **不自动重试**的 `DIRECT_GRID_FORCING_RESIDUE_CLEANUP_FAILED` 终止本次 attempt（目录形/软链形残留由
  `unlink_no_follow` 直接拒绝，落同一码），绝不在残留之上继续 staging；卫生之后仍并存的照旧两层 fail-closed。
  删除集恒为 `{canonical, legacy}` 减去 manifest 声明的部分，不触碰工作区其余任何文件。
- **非 direct-grid staging 的残余成员**：该路径是全前缀递归拷贝且 producer 从不清理，原地在
  pre-migration 前缀上再生产会留下孤儿 legacy 成员——属合法稳态，不 fail-closed。多成员时按声明源
  回退链解析：package manifest 发布非空 `files` 列表时以它为准，否则退到 run-manifest 的诊断
  `forcing.files`；所选声明源恰一命名可接受成员则用之，否则 canonical 兜底。
  全缺维持既有内部 forcing 回退。
- **staged 目的地不变**：runtime 读取成员后仍写出 `{project}.tsd.forc` 到模型输入目录，
  SHUD 模型侧命名（含 QHH 项目真资产 `data/Basins/qhh/input/qhh/qhh.tsd.forc`）不受影响。
- **legacy 分支截止条件**：当 object store 中不再存在缺少 canonical 成员的 direct-grid package
  （核验方式：扫描各 `forcing_package.json` 的 `files[].relative_path`，确认无 `shud/qhh.tsd.forc`），
  legacy 读侧分支可由后续 change 移除。不设代码定时器。
- **本次迁移只解决文件名/身份语义**：node-27 只读核验确认 18 个运行流域的 package 各自 SHA-256 不同、
  站点 bbox 各归其域，**未发现任何跨流域 forcing 数据污染**。

direct-grid lineage/package manifest 必须记录 `forcing_mapping_mode`、`spatial_mapping_method`、
binding URI/checksum、`model_input_package_id`、`.sp.att` path/checksum、`applicable_source_ids`、
`grid_id`、`grid_signature`、station identity 和 package file checksum。
Runtime 以 checksum 校验后的 `forcing_package.json` 为 direct-grid staging 权威，使用标准多站 package，
不执行 legacy 单站 `.sp.att` fallback rewrite。

### 5.6 迁移与回滚

迁移 direct-grid 的流程是发布新的模型/input 资产版本：先生成并校验 direct-grid binding 与 `.sp.att FORC`，再激活该资产版本供 forcing producer 使用。回滚同样通过选择上一版模型/input 资产完成，例如从 direct-grid 资产回到上一版 `idw` 资产，或回到上一版 binding/checksum 未漂移的 direct-grid 资产。不得修改历史 ready forcing version，也不得用 runtime 全局开关覆盖资产 manifest。

canonical conversion 对 IDW 和 direct-grid 都是必需前置步骤。IFS/GFS 原始 GRIB 中的降水和辐射累积量、温度单位、湿度来源、U/V 风分量、QC 与 lineage 必须先转成 canonical 产品；direct-grid 只改变空间查找方式，不绕过物理转换。

## 6. 主要接口

- `CLI nhms-forcing produce --source --cycle --model-id`
- `Slurm job produce_forcing_array`
- `GET /api/v1/forcing-versions/{id}`

## 7. 状态与错误

建议状态：

```text
created
running
succeeded
failed
skipped
superseded
```

建议错误码：

```text
INPUT_NOT_FOUND
INVALID_MANIFEST
PERMISSION_DENIED
OUTPUT_INCOMPLETE
QC_FAILED
STORAGE_WRITE_FAILED
UNKNOWN_ERROR
```

## 8. 观测性

每次执行至少记录：module_name、run_id/cycle_id/forcing_version_id/model_id、input_uri、output_uri、start_time、end_time、duration_seconds、status、error_code、log_uri。

## 9. 验收标准

- 输出 `.tsd.forc` 第一行站点数和开始日期正确。
- 每个 SHUD 站点 CSV 包含 Time_Day/Precip/Temp/RH/Wind/RN；Press 可作为站点时序或 lineage 元数据持久化，但不得写入 SHUD 站点 CSV 列。
- 时间轴覆盖 run_manifest 的 start/end。
- 任一站点缺失要素时 forcing_version 不得进入 ready。

## 10. 与其它模块的契约

- 输入对象必须处于 ready/published 状态，除非执行的是恢复或调试任务。
- 输出对象必须在 QC 通过后才能进入 ready/published 状态。
- 删除或替换产物必须通过新版本或 superseded 状态表达。
