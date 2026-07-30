# forecast-warm-start — delta for first-cycle-package-ic-consumption

## ADDED Requirements

### Requirement: A first-cycle basin SHALL receive exactly one auditable initial-state decision

For every basin × source whose generation history is empty at candidate planning time and whose model carries a published package-manifest reference (registry `resource_profile.manifest_uri`), the scheduler SHALL make exactly one machine-decidable initial-state decision on the generation-aware path before submission: (1) when the package manifest lists exactly one `*.cfg.ic` entry, that entry is the canonical one (`<shud_input_name>.cfg.ic` when the manifest names the SHUD input directory, otherwise a top-level path), its `sha256` differs from the empty-file digest and its `size_bytes` is positive, the packaged initial condition SHALL be selected and consumed (`PACKAGED_IC_BOOTSTRAP`); (2) when the entry is missing, non-canonical, ambiguous (more than one `*.cfg.ic` anywhere in the inventory), or empty (`UNQUALIFIED`), or the referenced manifest cannot be fetched or parsed (`UNREADABLE`), the candidate SHALL be blocked with a typed reason — an unreadable manifest SHALL NOT be treated as absence of an IC. Candidates that reach planning without a qualification signal SHALL retain today's behavior unchanged, in the shape that path produces today: a registry row that publishes no package-manifest reference yields the existing labeled `COLD_NEW_MODEL` decision (evidence mode `db_free_cold_new_model`, reason `no_prior_history`); the two named gate-bypass paths (registry entry without package checksum and declaration, and state-index-unavailable legacy fallback) yield the pre-existing legacy evidence shape, which carries no transition block and no packaged signal — strict mode blocks such first-cycle candidates, while non-strict mode may still reach the legacy `cold_start_no_state` selection fallback (a named residual, not new behavior). The decision SHALL be recorded in the candidate state evidence, the run manifest initial-state block, and the orchestration journal run row. The decision contract SHALL hold identically under strict and non-strict warm-start modes.

#### Scenario: Qualified packaged IC is selected and consumed

- **WHEN** a first-cycle basin's package manifest lists a `*.cfg.ic` entry with non-empty `sha256` and positive `size_bytes`, and the candidate is planned
- **THEN** the decision is `PACKAGED_IC_BOOTSTRAP`, candidate evidence mode is `db_free_packaged_ic_bootstrap`, and the run manifest carries `quality=packaged_calibrated_state`, `init_mode=3`, and the packaged IC `sha256`

#### Scenario: Unqualified packaged IC blocks

- **WHEN** a first-cycle basin's package manifest has no `*.cfg.ic` entry or the entry is empty
- **THEN** the candidate is blocked with the typed first-cycle-initial-state reason and nothing is submitted

#### Scenario: Ambiguous or non-canonical packaged IC entries block

- **WHEN** a first-cycle basin's package manifest lists more than one `*.cfg.ic` entry (for example a stray `CALIB/*.cfg.ic` next to the canonical one), or lists a single `*.cfg.ic` entry that is not the canonical top-level `<shud_input_name>.cfg.ic`
- **THEN** the candidate is blocked with the typed first-cycle-initial-state reason, no stray entry's digest is carried into the run manifest, and nothing is submitted into the runtime's exactly-one check

#### Scenario: Unreadable package manifest fails closed

- **WHEN** the referenced package manifest cannot be fetched or parsed at planning time
- **THEN** the candidate is blocked rather than classified as having no IC

#### Scenario: Missing qualification signal keeps legacy labeled cold start

- **WHEN** a first-cycle candidate's registry row publishes no package-manifest reference (and the candidate is not on a gate-bypass path)
- **THEN** the existing `COLD_NEW_MODEL` decision with evidence mode `db_free_cold_new_model` and reason `no_prior_history` applies unchanged

#### Scenario: Gate-bypass paths keep the pre-existing legacy evidence shape

- **WHEN** a first-cycle candidate reaches planning via a named gate-bypass path (no package checksum and no declaration, or state index unavailable)
- **THEN** the gate returns the pre-existing legacy evidence shape with no transition block and no packaged decision, exactly as before this change

#### Scenario: Decision is mode-independent

- **WHEN** a `PACKAGED_IC_BOOTSTRAP` candidate is admitted with `NHMS_REQUIRE_FORECAST_WARM_START` set to `true`, and again with it set to `false`
- **THEN** in both modes the produced run manifest carries `init_mode=3` and `quality=packaged_calibrated_state` — the packaged decision neither hard-fails strict admission nor degrades to `cold_start_no_state` in non-strict mode

#### Scenario: Existing-history basins are untouched

- **WHEN** a basin with non-empty generation history is planned
- **THEN** the observable outcomes are unchanged from before this change: `WARM_CONTINUE`/`COLD_DECLARED_CUTOVER` decisions, the four legitimate fallback qualities (`degraded_stale_init_state`, `cold_start_stale_state`, `cold_start_no_state`, corrupted-state re-query), their `init_mode` values, evidence mode strings, and error codes

### Requirement: Packaged-IC consumption SHALL fail closed at the runtime boundary

When a run manifest declares `quality=packaged_calibrated_state` — in either form: without a state id (scheduler-produced) or with a manual-manifest state id and no recorded packaged IC checksum (legacy form) — the SHUD runtime SHALL locate exactly one non-empty packaged `*.cfg.ic` in the staged model input and consume it or raise; the branch SHALL never fall through to an unlabeled cold start. When the manifest records a packaged IC `sha256`, the staged file's digest SHALL match it; when it does not (legacy manual manifest), the runtime SHALL verify non-emptiness and header parseability only and record a warning in run evidence. The runtime SHALL apply the standard negative-residual normalization before execution. Any failure — missing file, empty file, checksum mismatch, unparseable header, non-UTF-8/undecodable content, an unsafe-path or IO refusal, or a refused residual normalization — SHALL fail the run with the typed error `PACKAGED_IC_CONSUMPTION_FAILED`, carrying the originating reason in its message. Re-preparing the same workspace SHALL converge: a packaged IC whose basename differs from the project name is materialized to `<project_name>.cfg.ic`, and a repeated preparation SHALL drop that previous materialization before the exactly-one search instead of wedging the workspace. Packaged ICs are timeless calibration products: warm-start time-consistency verification and IC time shifting SHALL NOT be applied to them. Warm-start staging, its time-consistency verification, and runs that do not declare packaged-IC bootstrap SHALL remain byte-identical.

#### Scenario: Staged packaged IC is verified and consumed

- **WHEN** a packaged-IC bootstrap run stages the model package and the packaged `*.cfg.ic` matches the manifest-recorded `sha256` with a parseable header
- **THEN** the generated SHUD control file sets `INIT_MODE 3`, negative-residual normalization is applied, and the packaged file is the initial condition actually read by SHUD

#### Scenario: Legacy manual manifest without recorded checksum

- **WHEN** a manifest declares `packaged_calibrated_state` with a state id but no recorded packaged IC checksum, and the staged `*.cfg.ic` is non-empty with a parseable header
- **THEN** the run proceeds with `INIT_MODE 3` and a warning about the skipped checksum comparison is recorded in run evidence

#### Scenario: Consumption failure never becomes a silent cold start

- **WHEN** a run declaring `packaged_calibrated_state` (either form) finds the staged IC missing, empty, checksum-divergent, header-unparseable, or binary/undecodable
- **THEN** the run fails with `PACKAGED_IC_CONSUMPTION_FAILED` and no `INIT_MODE 1` execution occurs

#### Scenario: Re-preparing a workspace with a renamed packaged IC converges

- **WHEN** a workspace whose package ships its IC under a non-canonical basename is prepared twice (the second staging restores the source file next to the first attempt's `<project_name>.cfg.ic`)
- **THEN** the second preparation consumes the packaged IC again with an identical initial-state block and packaged evidence, exactly one `*.cfg.ic` remains in the model input, and `INIT_MODE 3` is still generated

### Requirement: A read-only audit SHALL reconcile packaged-IC qualification against first-run evidence

An operator-invoked read-only audit tool SHALL enumerate registered models × sources, determine packaged-IC qualification from the package manifest (`sha256`/`size_bytes` criteria), locate the earliest business run's manifest evidence, and emit a schema-versioned receipt classifying each row as `consumed_package_ic`, `cold_start_with_qualified_ic` (defect), `cold_start_no_ic`, or `undetermined` (evidence missing). The receipt SHALL carry a limits field stating that package objects are not re-hashed (manifest-recorded digests only). The tool SHALL NOT modify any production state, package, index, or journal content.

#### Scenario: Stock defect rows are reproduced

- **WHEN** the audit runs against the production registry and run evidence containing basins whose qualified packaged IC was not consumed on their first cycle
- **THEN** each such basin × source appears as a `cold_start_with_qualified_ic` row in the receipt and no file outside the receipt root is written

## MODIFIED Requirements

### Requirement: Forecast State Selection

系统 SHALL 在创建 forecast run 时自动选择最近可用 StateSnapshot 作为初始状态，并应用 freshness 检测规则。无可用 StateSnapshot 的 `cold_start_no_state` 回退仅适用于 generation 历史非空的流域；首时次流域(generation 历史为空)的初始状态决策由 first-cycle initial-state decision requirement 约束，不得经本回退静默冷启动——但该 requirement 中"无注册包引用"carve-out 情形的首时次候选仍按现行 labeled cold start 走本回退。

#### Scenario: Select latest usable state

- **WHEN** 创建 forecast run（model_id, cycle_time）且存在 usable_flag=true 的 state_snapshot（valid_time <= cycle_time），且 state 在 soft 阈值内（fresh）
- **THEN** hydro_run.init_state_id 设为该 state_id，manifest 中 initial_state.state_id 设为 state_id、initial_state.ic_file_uri 设为 state_uri

#### Scenario: Select stale state with degraded mark

- **WHEN** 创建 forecast run 且最近可用 state 的 valid_time 距 cycle_time 超过 soft 阈值但未超 hard 阈值
- **THEN** 仍使用该 state，hydro_run.init_state_id 设为 state_id，run_manifest 中 initial_state.quality='degraded_stale_init_state'

#### Scenario: State too old fallback to cold-start

- **WHEN** 创建 forecast run 且最近可用 state 超过 hard 阈值（默认 30 天）
- **THEN** hydro_run.init_state_id=NULL，runtime.init_mode=1，run_manifest 中 initial_state.quality='cold_start_stale_state'

#### Scenario: No usable state fallback to cold-start (existing-history basins)

- **WHEN** 创建 forecast run 但无可用 StateSnapshot，且该流域 generation 历史非空
- **THEN** hydro_run.init_state_id=NULL，runtime.init_mode=1，run_manifest 中 initial_state.state_id=null、initial_state.quality='cold_start_no_state'

### Requirement: SHUD Warm-start Configuration

系统 SHALL 在 workspace 准备阶段正确配置 SHUD warm-start 参数。INIT_MODE 的取值由 run manifest 的初始状态声明决定，而非仅由 init_state_id 决定。

#### Scenario: Warm-start .cfg.para generation

- **WHEN** init_state_id 不为空且 manifest 未声明 packaged-IC bootstrap
- **THEN** .cfg.para 中设置 INIT_MODE=3，initial_state.ic_file_uri 指向的 `.cfg.ic` 文件拷贝到 workspace 的正确位置

#### Scenario: Packaged-IC .cfg.para generation

- **WHEN** manifest 声明 `quality='packaged_calibrated_state'`（无论 state_id 是否为空）
- **THEN** .cfg.para 中设置 INIT_MODE=3，包内 `.cfg.ic` 按 fail-closed 消费 requirement 校验后作为初始条件

#### Scenario: Cold-start .cfg.para generation

- **WHEN** init_state_id 为空且 manifest 未声明 packaged-IC bootstrap
- **THEN** .cfg.para 中设置 INIT_MODE=1，无需 `.cfg.ic` 文件

### Requirement: Init State Validation

系统 SHALL 在 forecast run 启动前验证 init_state 文件完整性。本 requirement 的 checksum 校验与"标记 unusable 后重查/回退"语义仅适用于 **state-snapshot 来源**的初始状态（initial_state.ic_file_uri 指向 state_snapshot 对象）；包内 packaged IC 的校验与失败语义由 packaged-IC fail-closed 消费 requirement 独立约束，不适用本回退。

#### Scenario: Init state file valid

- **WHEN** initial_state.ic_file_uri 指向的 `.cfg.ic` 文件存在且 checksum 与 state_snapshot 记录匹配
- **THEN** 继续启动 SHUD

#### Scenario: Init state file corrupted

- **WHEN** `.cfg.ic` 文件 checksum 不匹配或文件不存在
- **THEN** 系统标记该 state_snapshot.usable_flag=false，记录 error_code='INIT_STATE_CORRUPTED'，重新查询下一个最近可用状态；如果无可用状态则 fallback cold-start
- **AND** 上述 fallback 仅适用于非严格兼容模式；严格业务模式必须保留原 exact-state 身份并失败闭锁，等待修复或重试，不得改用旧状态或 cold-start

### Requirement: Run Manifest Init State Fields

系统 SHALL 在 run_manifest 中使用嵌套结构包含 init_state 相关字段，遵循 Appendix B manifest schema。

#### Scenario: Manifest with warm-start

- **WHEN** forecast run 使用 warm-start
- **THEN** run_manifest JSON 包含 `initial_state: { state_id, ic_file_uri }` 和 `runtime: { init_mode: 3 }`

#### Scenario: Manifest with cold-start

- **WHEN** forecast run 使用 cold-start
- **THEN** run_manifest JSON 包含 `initial_state: { state_id: null, ic_file_uri: null, quality: "<reason>" }` 和 `runtime: { init_mode: 1 }`

#### Scenario: Manifest with packaged-IC bootstrap

- **WHEN** forecast run 使用 packaged-IC bootstrap
- **THEN** run_manifest JSON 包含 `initial_state: { state_id: null, ic_file_uri: null, quality: "packaged_calibrated_state", packaged_ic_checksum: "<sha256>" }` 和 `runtime: { init_mode: 3 }`
