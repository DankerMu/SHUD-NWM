## MODIFIED Requirements

### Requirement: Compression runner timeout budget chain MUST be operator-configurable and fail closed

The node-27 timeseries compression runner SHALL derive its per-chunk `statement_timeout`, its wrapper wall, and its declared systemd wall from operator-configurable environment variables sourced from the single compression env file, with defaults of 3600000 ms (per-chunk statement timeout), 3900 s (wrapper wall), and 3940 s (declared systemd wall) — recalibrated by #1352 from the former hardcoded 840000 ms / 900 s / 940 s against measured steady-state chunk compression rates — and SHALL reject any configuration that violates either leg of the budget-chain invariant — per-chunk timeout in seconds (rounded up) plus the fixed cleanup margin must not exceed the wrapper wall, and the wrapper wall plus the fixed kill-after margin must not exceed the declared systemd wall — before opening any database connection. The invariant bounds a single chunk's budget, not a whole tick.

Since #2713 the runner SHALL also derive the bounded wait for the compression/ingest advisory fence, taken before each `compress_chunk`, from `NODE27_TIMESERIES_COMPRESSION_FENCE_WAIT_MS` in the same env file (default 900000 ms), SHALL charge the measured fence wait against that chunk's per-chunk timeout so the session `statement_timeout` of `compress_chunk` is the remainder, and SHALL reject a fence wait that fails positive-integer validation or is not strictly less than the per-chunk timeout before opening any database connection. Because the wait is charged inside the per-chunk timeout, both legs of the budget-chain invariant are unchanged.

#### Scenario: defaults unchanged

WHEN none of the timeout environment variables is set, or any of them is set to the empty string
THEN the runner uses 3600000 ms as the per-chunk statement timeout, 3900 s as the wrapper wall, and 3940 s as the declared systemd wall
AND runtime behavior and the receipt schema are identical to the no-override configuration.

#### Scenario: override propagates to the database session

WHEN `NODE27_TIMESERIES_COMPRESSION_COMPRESS_TIMEOUT_MS` is set to a valid value satisfying the budget-chain invariant
THEN every `compress_chunk` call session issues `SET statement_timeout` with exactly the overridden value minus the fence wait measured in that session (at least 1 ms)
AND the fence wait itself runs under `SET lock_timeout` equal to the configured fence wait, before any copy starts.

#### Scenario: budget-chain violation is rejected before any DB call

WHEN the configured per-chunk timeout plus the cleanup margin exceeds the configured wrapper wall, or the wrapper wall plus the kill-after margin exceeds the declared systemd wall, or any of the three variables fails positive-integer validation, or `NODE27_TIMESERIES_COMPRESSION_FENCE_WAIT_MS` fails positive-integer validation or is not strictly less than the per-chunk timeout
THEN the runner raises a fail-closed configuration error naming the violated invariant
AND no database connection is attempted.

#### Scenario: wrapper wall guard is fail-closed

WHEN the wrapper wall environment variable is present, non-empty, and not a positive integer
THEN the shell wrapper exits non-zero with a structured error before executing the runner
AND when the variable is absent the wrapper uses the 3900 s default.

#### Scenario: lowering the compress timeout under the default fence wait

WHEN `NODE27_TIMESERIES_COMPRESSION_COMPRESS_TIMEOUT_MS` is set to 900000 or less and `NODE27_TIMESERIES_COMPRESSION_FENCE_WAIT_MS` is unset
THEN the runner refuses the configuration before any database connection, naming the fence-wait variable, until the operator sets a fence wait below the new per-chunk timeout.

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

#2713 起 runner 写 schema_version "2.2"：`budget` 对象 MUST 另携带本次生效的
`fence_wait_ms`（2.2 必填，1.0/2.0/2.1 禁止），其余字段与 all-or-nothing、config
tombstone 唯一缺省、非 failed receipt 必带 `head_sha` 的规则对 "2.2" 同样适用；
`deferred_contended_count`、`mutation_state == "deferred_contended"`、
`fence_wait_elapsed_ms` 与 outcome `deferred` 仅 "2.2" 合法。

#### Scenario: 非默认预算如实落 receipt

- **WHEN** operator 以非默认预算运行（如 1800000 ms / 1900 s / 1940 s，bound=1，fence wait 600000 ms）
- **THEN** 当次 receipt `budget` 各字段（含 `fence_wait_ms`）逐一等于该非默认值，`schema_version == "2.2"`，
  与默认预算 receipt 字节可区分

#### Scenario: 半截 budget 被 schema 拒绝

- **WHEN** receipt 携带只含一或两个字段的 `budget` 对象，或 "2.2" receipt 的 `budget` 缺 `fence_wait_ms`
- **THEN** schema 校验失败（all-or-nothing 由 `budget` 定义的 required 全列 +
  additionalProperties:false 强制，"2.2" 另由版本条件要求 `fence_wait_ms`）

#### Scenario: config tombstone 是唯一合法缺省

- **WHEN** `config_from_args` 抛错且存在 stale receipt，early tombstone 被写出
- **THEN** 该 receipt `schema_version == "2.2"`、无 `budget`，schema 校验通过；
  任何其它 2.1/2.2 形状缺 `budget` 均校验失败

#### Scenario: 历史 receipt 保持可验证

- **WHEN** live-evidence 用更新后的 schema 校验历史 1.0/2.0 receipt（无 budget）与历史 2.1 receipt（budget 无 `fence_wait_ms`）
- **THEN** 校验通过；1.0/2.0 receipt 若被注入 `budget` 则校验失败，1.0/2.0/2.1 receipt 若被注入任何 #2713 字段则校验失败
