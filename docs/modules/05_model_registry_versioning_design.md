# 05. 模型资产与版本管理模块：模块设计

版本：v0.2  
日期：2026-05-06

## 1. 模块目标

管理流域、流域版本、mesh、河网、率定参数、SHUD/rSHUD/AutoSHUD 版本和模型包。

## 2. 上下游关系

| 方向 | 内容 |
|---|---|
| 上游 | 模型构建团队、rSHUD/AutoSHUD 产物、空间数据。 |
| 下游 | Forcing Producer、SHUD Runtime、Flood Frequency Engine、前端图层。 |
| 主要数据表/存储 | `core.basin`, `core.basin_version`, `core.model_instance`, `core.river_segment`, `core.river_segment_crosswalk` |

## 3. 职责边界

- 注册 basin_version、river_network_version、mesh_version。
- 注册 model_instance 和 resource_profile。
- 管理 active/deprecated 状态。
- 维护 river_segment_crosswalk。
- 校验模型包完整性。

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

### 5.4 Forcing mapping 资产版本

模型/input 资产 manifest 可以声明 `forcing_mapping_mode`：

- 缺省或 `idw` 表示 legacy 模式，模型包保留原 SHUD/rSHUD forcing station 与 `.att FORC` 覆盖，Forcing Producer 在运行期把 canonical 气象场插值到这些固定站点。
- `direct_grid` 表示模型包已经迁移到 direct-grid station ownership。该版本必须携带 direct-grid binding URI/checksum、`model_input_package_id`、`.sp.att` path/checksum、`applicable_source_ids`、`grid_id`、`grid_signature` 和每站 `shud_forcing_index`/`grid_cell_id`/`forcing_filename`。

这些字段共同定义 direct-grid 适用范围。`applicable_source_ids` 限定可复用该 binding 的 source；`grid_id` 只是名称，`grid_signature` 才是有序格点定义的身份。任一 source scope、grid signature、binding checksum、模型 input package identity 或 `.sp.att` checksum 变化，都必须发布新资产版本。

### 5.5 direct-grid 迁移与回滚

direct-grid 迁移不通过全局配置开关完成，而是发布并激活新的模型/input 资产版本：

1. 基于目标 IFS/GFS canonical 网格生成 direct-grid binding。
2. 重算模型 `.sp.att FORC`，使所有元素引用 binding 中的 `shud_forcing_index`。
3. 记录 binding checksum、`.sp.att` checksum、`model_input_package_id`、source scope 和 grid signature。
4. 通过 Model Registry 激活该新版本。

回滚也只通过版本选择表达：把 active model/input asset 切回上一版 `idw` 资产，或切回上一版已验证的 direct-grid 资产。不得就地修改历史模型包、不得改写已 ready 的 forcing version，也不得让 SHUD Runtime 用 fallback rewrite 覆盖 direct-grid `.sp.att`。

## 6. 主要接口

- `POST /api/v1/models`
- `GET /api/v1/models`
- `POST /api/v1/models/{id}/activate`
- `CLI nhms-model validate-package`

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

- 新模型注册后默认 inactive。
- active 切换必须有审计日志。
- 模型包包含 SHUD 必需输入文件。
- 流域边界变化后历史结果仍可按旧版本查询。

## 10. 与其它模块的契约

- 输入对象必须处于 ready/published 状态，除非执行的是恢复或调试任务。
- 输出对象必须在 QC 通过后才能进入 ready/published 状态。
- 删除或替换产物必须通过新版本或 superseded 状态表达。
