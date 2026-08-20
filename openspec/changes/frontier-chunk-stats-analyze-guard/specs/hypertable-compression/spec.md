# hypertable-compression

## MODIFIED Requirements

### Requirement: Compression receipts MUST record the effective timeout/wall budget chain

压缩 receipt（schema_version "2.1" 起）MUST 携带 `budget` 对象
`{compress_timeout_ms, wrapper_wall_seconds, systemd_wall_seconds}`，数值等于本次运行
`CompressionConfig` 实际生效值，三字段 all-or-nothing。唯一合法缺省形态是
provenance-unavailable config tombstone（`outcome == "failed"` 且
`failure.stage == "config"` 且 `per_tick_bound` 缺失——结构性 config-absence 双判别）
——该路径上从未存在合法 config，禁止补发任何预算值。
schema_version "1.0"/"2.0" 的 receipt 禁止携带 `budget`；schema_version "2.1"/"2.2"
的非 failed receipt 仍 MUST 携带 `head_sha`（既有 provenance 钉随版本放宽同步保留）。
runner 当前发射版本为 "2.2"（frontier-chunk-stats-analyze-guard 起：chunk 条目新增
`analyze_seconds`/`analyze_error`，仅在 2.2 合法）。
消费侧（live-evidence）双冻结契约保持硬编码：`EXPECTED_TIMEOUT_SECONDS = 900` 与
`verify_bundle` 对 #1069 冻结 bundle 的 `schema_version == "2.0"` 语义钉，
均禁止改为跟随新字段/新版本。

#### Scenario: 非默认预算如实落 receipt

- **WHEN** operator 以非默认预算运行（如 1800000 ms / 1900 s / 1940 s，bound=1）
- **THEN** 当次 receipt `budget` 三字段逐一等于该非默认值，`schema_version == "2.2"`
  （当前发射版本），与默认预算 receipt 字节可区分

#### Scenario: 半截 budget 被 schema 拒绝

- **WHEN** receipt 携带只含一或两个字段的 `budget` 对象
- **THEN** schema 校验失败（all-or-nothing 由 `budget` 定义的 required 全列 +
  additionalProperties:false 强制）

#### Scenario: config tombstone 是唯一合法缺省

- **WHEN** `config_from_args` 抛错且存在 stale receipt，early tombstone 被写出
- **THEN** 该 receipt `schema_version == "2.2"`（当前发射版本）、无 `budget`，
  schema 校验通过；任何其它 2.1/2.2 形状缺 `budget` 均校验失败

#### Scenario: 历史 receipt 保持可验证

- **WHEN** live-evidence 用更新后的 schema 校验历史 1.0/2.0/2.1 receipt
  （按各自版本形状）
- **THEN** 校验通过；1.0/2.0 receipt 若被注入 `budget`、或 1.0/2.0/2.1 receipt
  若被注入 `analyze_seconds`/`analyze_error` 则校验失败
