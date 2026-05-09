## ADDED Requirements

### Requirement: Forecast State Selection
系统 SHALL 在创建 forecast run 时自动选择最近可用 StateSnapshot 作为初始状态，并应用 freshness 检测规则。

#### Scenario: Select latest usable state
- **WHEN** 创建 forecast run（model_id, cycle_time）且存在 usable_flag=true 的 state_snapshot（valid_time <= cycle_time），且 state 在 soft 阈值内（fresh）
- **THEN** hydro_run.init_state_id 设为该 state_id，manifest 中 initial_state.state_id 设为 state_id、initial_state.ic_file_uri 设为 state_uri

#### Scenario: Select stale state with degraded mark
- **WHEN** 创建 forecast run 且最近可用 state 的 valid_time 距 cycle_time 超过 soft 阈值但未超 hard 阈值
- **THEN** 仍使用该 state，hydro_run.init_state_id 设为 state_id，run_manifest 中 initial_state.quality='degraded_stale_init_state'

#### Scenario: State too old fallback to cold-start
- **WHEN** 创建 forecast run 且最近可用 state 超过 hard 阈值（默认 30 天）
- **THEN** hydro_run.init_state_id=NULL，runtime.init_mode=1，run_manifest 中 initial_state.quality='cold_start_stale_state'

#### Scenario: No usable state fallback to cold-start
- **WHEN** 创建 forecast run 但无可用 StateSnapshot
- **THEN** hydro_run.init_state_id=NULL，runtime.init_mode=1，run_manifest 中 initial_state.state_id=null、initial_state.quality='cold_start_no_state'

### Requirement: SHUD Warm-start Configuration
系统 SHALL 在 workspace 准备阶段正确配置 SHUD warm-start 参数。

#### Scenario: Warm-start .cfg.para generation
- **WHEN** init_state_id 不为空
- **THEN** .cfg.para 中设置 INIT_MODE=3，initial_state.ic_file_uri 指向的 `.cfg.ic` 文件拷贝到 workspace 的正确位置

#### Scenario: Cold-start .cfg.para generation
- **WHEN** init_state_id 为空
- **THEN** .cfg.para 中设置 INIT_MODE=1，无需 `.cfg.ic` 文件

### Requirement: Init State Validation
系统 SHALL 在 forecast run 启动前验证 init_state 文件完整性。

#### Scenario: Init state file valid
- **WHEN** initial_state.ic_file_uri 指向的 `.cfg.ic` 文件存在且 checksum 与 state_snapshot 记录匹配
- **THEN** 继续启动 SHUD

#### Scenario: Init state file corrupted
- **WHEN** `.cfg.ic` 文件 checksum 不匹配或文件不存在
- **THEN** 系统标记该 state_snapshot.usable_flag=false，记录 error_code='INIT_STATE_CORRUPTED'，重新查询下一个最近可用状态；如果无可用状态则 fallback cold-start

### Requirement: Run Manifest Init State Fields
系统 SHALL 在 run_manifest 中使用嵌套结构包含 init_state 相关字段，遵循 Appendix B manifest schema。

#### Scenario: Manifest with warm-start
- **WHEN** forecast run 使用 warm-start
- **THEN** run_manifest JSON 包含 `initial_state: { state_id, ic_file_uri }` 和 `runtime: { init_mode: 3 }`

#### Scenario: Manifest with cold-start
- **WHEN** forecast run 使用 cold-start
- **THEN** run_manifest JSON 包含 `initial_state: { state_id: null, ic_file_uri: null, quality: "<reason>" }` 和 `runtime: { init_mode: 1 }`
