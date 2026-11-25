## ADDED Requirements

### Requirement: The compress statement SHALL bound every lock wait and report a lock-wait timeout as contention

压缩会话在取得 fence 之后、执行 `compress_chunk` 之前 MUST 设置 `lock_timeout`，其值为
`NODE27_TIMESERIES_COMPRESSION_LOCK_TIMEOUT_MS`（正整数；未声明时取代码内默认 5000）。该值 MUST
严格小于 `compress_timeout_ms - fence_wait_ms`，否则 runner 以配置错误 fail closed，不连库。
未声明该键的 env 文件 MUST 继续可用。`compress_chunk` 因等锁超时（SQLSTATE 55P03）而失败时，
runner MUST 先做与其它失败相同的只读 reconcile：chunk 未压缩则记为
`mutation_state == "deferred_contended"`、`contended_on == "relation_lock"`，计入
`deferred_contended_count`，不记 error、不污染该表的 `after_bytes`，其余只有争用时 outcome 为
`deferred`（exit 0）；reconcile 结果为已压缩或 reconcile 自身失败时，沿用既有的 `committed` /
`indeterminate` 处理。语句超时（57014）仍是失败（`failed_before_mutation`，outcome `partial`）。
fence 的获取与其 `lock_timeout` 处理不变。

原因：`compress_chunk` 在重写完数据后要对未压缩 chunk 取 AccessExclusive；被一个持有
ACCESS SHARE 的未结束事务挡住时，之后到达的读查询会排在它后面。2026-11-19 这样等了 52 分钟，
期间 7 条读查询被各自的语句超时取消。

#### Scenario: 等锁超时被记为争用并放行排队的读

- **WHEN** 一个未结束的事务持有某 chunk 的 ACCESS SHARE，而压缩会话以 `lock_timeout` 压缩该 chunk
- **THEN** `compress_chunk` 在约 `lock_timeout` 后以 55P03 结束，chunk 保持未压缩，
  receipt 中该 chunk 为 `deferred_contended` 且 `contended_on == "relation_lock"`，
  此后同一 tick 的下一个 chunk 仍被尝试

#### Scenario: 语句超时仍是失败

- **WHEN** `compress_chunk` 被 `statement_timeout` 取消
- **THEN** 该 chunk 为 `failed_before_mutation`，outcome 为 `partial`，退出码非零

#### Scenario: 不合法的 lock timeout 配置 fail closed

- **WHEN** `NODE27_TIMESERIES_COMPRESSION_LOCK_TIMEOUT_MS` 不是正整数，或不小于
  `compress_timeout_ms - fence_wait_ms`
- **THEN** runner 以配置错误结束，未连接数据库

#### Scenario: 未声明该键的 env 继续可用

- **WHEN** env 文件没有 `NODE27_TIMESERIES_COMPRESSION_LOCK_TIMEOUT_MS`
- **THEN** runner 以 5000 ms 运行，receipt 的 `budget.lock_timeout_ms` 为 5000

### Requirement: The compression unit SHALL alert on failure through the node-27 unit-failure channel

`nhms-node27-timeseries-compression.service` MUST 声明
`OnFailure=nhms-node27-unit-failure-alert@%n.service`，与 governance / retention unit 同一通道。
unit 的 `ExecStartPre` preflight 与唯一 `ExecStart` 的次序、`TimeoutStartSec`、stdout / stderr
的去向不变；runbook MUST 写明该 unit 的 stderr 在文件里而不在 journal 里。

#### Scenario: 失败触发告警 unit

- **WHEN** 压缩 unit 以非零退出（outcome `partial`、`failed`（含配置 tombstone），或 preflight 失败）
- **THEN** systemd 启动 `nhms-node27-unit-failure-alert@nhms-node27-timeseries-compression.service.service`

#### Scenario: 争用延期与生命周期锁被占不告警

- **WHEN** tick 的 outcome 为 `deferred` 或 `refused_lock`
- **THEN** unit 以 0 退出，不触发告警

## MODIFIED Requirements

### Requirement: Compression receipts MUST record the effective timeout/wall budget chain

压缩 receipt（schema_version "2.1" 起）MUST 携带 `budget` 对象
`{compress_timeout_ms, wrapper_wall_seconds, systemd_wall_seconds}`，数值等于本次运行
`CompressionConfig` 实际生效值，三字段 all-or-nothing。唯一合法缺省形态是
provenance-unavailable config tombstone（`outcome == "failed"` 且
`failure.stage == "config"` 且 `per_tick_bound` 缺失——结构性 config-absence 双判别）
——该路径上从未存在合法 config，禁止补发任何预算值。
schema_version "1.0"/"2.0" 的 receipt 禁止携带 `budget`；schema_version "2.1" 的非
failed receipt 仍 MUST 携带 `head_sha`（既有 provenance 钉随版本放宽同步保留）。
消费侧（live-evidence）双冻结契约保持硬编码：`EXPECTED_TIMEOUT_SECONDS = 900` 与
`verify_bundle` 对 #1069 冻结 bundle 的 `schema_version == "2.0"` 语义钉，
均禁止改为跟随新字段/新版本。

自 #2713 起 runner 写 schema_version "2.2"：`budget` 对象 MUST 另携带本次生效的
`fence_wait_ms`（2.2 必填，1.0/2.0/2.1 禁止），其余字段与 all-or-nothing、config
tombstone 唯一缺省、非 failed receipt 必带 `head_sha` 的规则对 "2.2" 同样适用；
`deferred_contended_count`、`mutation_state == "deferred_contended"`、
`fence_wait_elapsed_ms` 与 outcome `deferred` 自 "2.2" 起合法（"2.2" 与 "2.3"）。

自 #3380 起 runner 写 schema_version "2.3"：`budget` 对象 MUST 另携带本次生效的
`lock_timeout_ms`（2.3 必填，更早版本禁止）；每个 `mutation_state == "deferred_contended"`
的 chunk 描述 MUST 携带 `contended_on`，取值 `fence`、`relation_lock`、`tick_deadline`
之一（2.3 必填，更早版本禁止）。"2.2" 的其余规则对 "2.3" 同样适用。

#### Scenario: 非默认预算如实落 receipt

- **WHEN** operator 以非默认预算运行（如 1800000 ms / 1900 s / 1940 s，bound=1，fence wait 600000 ms，lock timeout 2000 ms）
- **THEN** 当次 receipt `budget` 各字段（含 `fence_wait_ms` 与 `lock_timeout_ms`）逐一等于该非默认值，`schema_version == "2.3"`，
  与默认预算 receipt 字节可区分

#### Scenario: 半截 budget 被 schema 拒绝

- **WHEN** receipt 携带只含一或两个字段的 `budget` 对象，或 "2.2"/"2.3" receipt 的 `budget` 缺 `fence_wait_ms`
- **THEN** schema 校验失败（all-or-nothing 由 `budget` 定义的 required 全列 +
  additionalProperties:false 强制，"2.2" 起另由版本条件要求 `fence_wait_ms`）

#### Scenario: config tombstone 是唯一合法缺省

- **WHEN** `config_from_args` 抛错且存在 stale receipt，early tombstone 被写出
- **THEN** 该 receipt `schema_version == "2.3"`、无 `budget`，schema 校验通过；
  任何其它 2.1/2.2/2.3 形状缺 `budget` 均校验失败

#### Scenario: 历史 receipt 保持可验证

- **WHEN** live-evidence 用更新后的 schema 校验历史 1.0/2.0 receipt（无 budget）、历史 2.1 receipt（budget 无 `fence_wait_ms`）
  与历史 2.2 receipt（budget 无 `lock_timeout_ms`，描述无 `contended_on`）
- **THEN** 校验通过；1.0/2.0 receipt 若被注入 `budget` 则校验失败，1.0/2.0/2.1 receipt 若被注入任何 #2713 字段则校验失败，
  2.2 及更早的 receipt 若被注入 `lock_timeout_ms` 或 `contended_on` 则校验失败

#### Scenario: 2.3 receipt 必带 lock_timeout_ms 与 contended_on

- **WHEN** "2.3" receipt 的 `budget` 缺 `lock_timeout_ms`，或某个 `deferred_contended` 描述缺 `contended_on`
- **THEN** schema 校验失败
