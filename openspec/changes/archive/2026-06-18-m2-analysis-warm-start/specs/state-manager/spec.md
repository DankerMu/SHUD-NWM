## ADDED Requirements

### Requirement: StateSnapshot Storage
系统 SHALL 将 SHUD `.cfg.ic` 状态文件存储到对象存储，元数据写入 hydro.state_snapshot 表。

#### Scenario: Save state snapshot
- **WHEN** analysis run 成功并输出 `.cfg.ic` 文件
- **THEN** 系统将文件上传到 `states/{model_id}/{valid_time}/state.cfg.ic`，写入 hydro.state_snapshot（state_id='state_{model_id}_{valid_time}', model_id, run_id, valid_time, state_uri, checksum=SHA256, usable_flag=false）

#### Scenario: Idempotent state save (same checksum)
- **WHEN** 同一 (model_id, valid_time) 的 state_snapshot 已存在且 checksum 匹配
- **THEN** 系统返回 already_done，不重复上传

#### Scenario: Conflict state save (different checksum)
- **WHEN** 同一 (model_id, valid_time) 的 state_snapshot 已存在但 checksum 不同（来自不同 analysis run）
- **THEN** 系统将旧记录标记为 superseded（不删除），创建新 state_snapshot 记录（新 state_id 含 run_id 后缀消歧），新记录 usable_flag=false 待 QC

#### Scenario: Atomic visibility during upload
- **WHEN** state snapshot 正在上传（文件写入对象存储 + DB 写入 + QC 进行中）
- **THEN** usable_flag 保持 false 直到全部步骤完成，forecast 查询最近可用状态时不会看到部分写入的 state

### Requirement: StateSnapshot Usable Flag Management
系统 SHALL 通过 usable_flag 控制 StateSnapshot 是否可用于 forecast warm-start。

#### Scenario: QC pass enables state
- **WHEN** state snapshot QC 检查通过（文件存在、大小 > 0、checksum 匹配）
- **THEN** 系统将 usable_flag 设为 true，写入 ops.qc_result 记录

#### Scenario: QC fail keeps state unusable
- **WHEN** state snapshot QC 检查失败（文件不存在、大小为 0 或 checksum 不匹配）
- **THEN** usable_flag 保持 false，写入 ops.qc_result 记录（含 error_code）

### Requirement: Query Latest Usable State
系统 SHALL 提供查询指定 model 最近可用 StateSnapshot 的接口。

#### Scenario: Latest usable state found
- **WHEN** 查询 model_id='yangtze_shud_v12' 在 valid_time <= '2026-04-30T00:00:00Z' 的最近可用状态
- **THEN** 返回 usable_flag=true 且 valid_time 最大的 state_snapshot（state_id, state_uri, valid_time, checksum）

#### Scenario: Multiple usable states deterministic selection
- **WHEN** 存在多个 usable_flag=true 的 state（如 valid_time=04-28 和 valid_time=04-29）
- **THEN** 确定性返回 valid_time 最大的那个（04-29），不受创建顺序影响

#### Scenario: No usable state available
- **WHEN** 查询 model_id 的可用状态但无记录（首次运行）
- **THEN** 返回 None/null，调用方应 fallback 到 cold-start

### Requirement: State Freshness Detection
系统 SHALL 检测 StateSnapshot 是否过旧，用于 forecast run 降级标记。

#### Scenario: State is fresh
- **WHEN** 最近可用 state 的 valid_time 距离 forecast cycle_time <= 配置 soft 阈值（默认 7 天）
- **THEN** forecast run 正常使用该 state，run_manifest 中无 degraded 标记

#### Scenario: State is stale (soft threshold)
- **WHEN** 最近可用 state 的 valid_time 距离 forecast cycle_time > soft 阈值（默认 7 天）且 <= hard 阈值（默认 30 天）
- **THEN** forecast run 仍使用该 state，但 run_manifest 中标记 init_state_quality='degraded_stale_init_state'

#### Scenario: State staleness hard threshold exceeded
- **WHEN** 最近可用 state 的 valid_time 距离 forecast cycle_time > hard 阈值（默认 30 天）
- **THEN** 系统拒绝使用该 state，fallback 到 cold-start，run_manifest 中标记 init_state_quality='cold_start_stale_state'

### Requirement: State Snapshot API
系统 SHALL 提供 REST API 查询 StateSnapshot。

#### Scenario: List state snapshots
- **WHEN** 请求 GET /api/v1/state-snapshots?model_id=xxx&usable=true
- **THEN** 返回按 valid_time DESC 排序的 state_snapshot 列表，支持分页

#### Scenario: Get single state snapshot
- **WHEN** 请求 GET /api/v1/state-snapshots/{state_id}
- **THEN** 返回 state_snapshot 详情（state_id, model_id, run_id, valid_time, state_uri, checksum, usable_flag, created_at）
