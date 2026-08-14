# hypertable-compression（delta）

## ADDED Requirements

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

#### Scenario: 非默认预算如实落 receipt

- **WHEN** operator 以非默认预算运行（如 1800000 ms / 1900 s / 1940 s，bound=1）
- **THEN** 当次 receipt `budget` 三字段逐一等于该非默认值，`schema_version == "2.1"`，
  与默认预算 receipt 字节可区分

#### Scenario: 半截 budget 被 schema 拒绝

- **WHEN** receipt 携带只含一或两个字段的 `budget` 对象
- **THEN** schema 校验失败（all-or-nothing 由 `budget` 定义的 required 全列 +
  additionalProperties:false 强制）

#### Scenario: config tombstone 是唯一合法缺省

- **WHEN** `config_from_args` 抛错且存在 stale receipt，early tombstone 被写出
- **THEN** 该 receipt `schema_version == "2.1"`、无 `budget`，schema 校验通过；
  任何其它 2.1 形状缺 `budget` 均校验失败

#### Scenario: 历史 receipt 保持可验证

- **WHEN** live-evidence 用更新后的 schema 校验历史 1.0/2.0 receipt（无 budget）
- **THEN** 校验通过；同版本 receipt 若被注入 `budget` 则校验失败

### Requirement: Raising the compress timeout above default MUST fail closed unless per_tick_bound is 1

`config_from_args` MUST 在 `compress_timeout_ms > 默认值（3600000 ms）` 且
`per_tick_bound > 1` 时抛 `CompressionConfigError`（pre-connect，零 DB 调用），
错误文案指向 runbook §4.5 的追赶配方（抬墙必须 `PER_TICK_BOUND=1`）。
等于或低于默认值的 timeout 不触发本约束。本约束**只守 §4.5 追赶窗口的显式抬墙操作**；
默认 timeout 下 bound=4 遇 ≥2 river chunk 的撞墙险 config 时刻不可见（chunk 尺寸未知），
仍按 runbook §4 的 operator 检测权威处置——不得宣称本约束覆盖该残差。

#### Scenario: 抬墙未降 bound 被拒

- **WHEN** env 设 `COMPRESS_TIMEOUT_MS=7200000` 且 `PER_TICK_BOUND=4`
- **THEN** runner 在任何 DB 连接前以 `CompressionConfigError` 退出，文案含 §4.5 指针

#### Scenario: 合法追赶组合与默认组合不受影响

- **WHEN** env 设抬墙 + `PER_TICK_BOUND=1`，或默认 timeout + `PER_TICK_BOUND=4`
- **THEN** config 构造成功，既有 budget-chain 两腿不变量行为不变
