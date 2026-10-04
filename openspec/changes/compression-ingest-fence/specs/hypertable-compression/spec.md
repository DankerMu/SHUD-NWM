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

### Requirement: A recompute blocked by the compressed-chunk guard MUST reach a recorded terminal state instead of retrying forever

一次被压缩块守卫拒绝的重算 SHALL 达到一个被记录的终态，而不是无限重试。具体地：当 ingest tick 正确检出一次产物重算、而该重算的写入被 `check_batch_targets_uncompressed` 以 `HANDOFF_APPLY_COMPRESSED_CHUNK_BLOCKED` 拒绝时，该 run SHALL 被记入一条终态 decline 记录并停止重投，tick SHALL 以 `rc=0` 结束。终态记录 SHALL 以 `(run_id, init_state_id, product_mtime)` 为键，使任何新的
重算证据自动重开该决定。记账 SHALL 是可查询的持久状态，而非仅一行日志。

#### Scenario: A compressed-chunk-blocked recompute is declined, not failed

- **WHEN** ingest tick 处理一个 run，其 forcing handoff 返回的 reason codes 含
  `HANDOFF_APPLY_COMPRESSED_CHUNK_BLOCKED`
- **THEN** `_process_run` 返回 `outcome="declined"`（不是 `"failed"`），
  `ops.ingest_recompute_decline` 新增一行 `(run_id, init_state_id, product_mtime,
  reason_code='HANDOFF_APPLY_COMPRESSED_CHUNK_BLOCKED')`，
  tick 汇总的 `runs.declined_runs` 含该 run_id，且进程 `rc == 0`

#### Scenario: A transient forcing failure still fails the tick and retries

- **WHEN** forcing handoff 因任何非 `HANDOFF_APPLY_COMPRESSED_CHUNK_BLOCKED`
  的原因失败（含通用异常路径与 `HANDOFF_APPLY_SQL_FAILURE`），且其 reason codes
  不含非失败的 skip 码 `HANDOFF_APPLY_LEGACY_STORE_REFUSED`（legacy 路由的
  forcing_version，#1991 I12）或 `HANDOFF_APPLY_COMPRESSION_FENCE_BUSY`（#2713
  压缩/ingest fence 被 chunk DDL 持有或排队）——这两者 `_process_run` 返回
  `outcome="skipped"`、不写 decline 行、不计入 tick rc
- **THEN** `_process_run` 返回 `outcome="failed"`，`ops.ingest_recompute_decline`
  不新增任何行，进程 `rc == 1`，该 run 在下个 tick 仍进入 pending

#### Scenario: A compression-fence-busy forcing apply is skipped, not failed

- **WHEN** forcing handoff 的 reason codes 含 `HANDOFF_APPLY_COMPRESSION_FENCE_BUSY`
  （apply 在任何语句之前回滚，未写入任何表）
- **THEN** `_process_run` 返回 `outcome="skipped"`、`reason` 为该码，
  `ops.ingest_recompute_decline` 不新增任何行，该 run 不计入 tick rc，
  且因未 ingest 而在下个 tick 重新进入 pending 并重试

#### Scenario: The second tick does not retry a declined run, at any hydro_run status

- **WHEN** 一个 run 已有键完全匹配当前 manifest `initial_state.state_id` 与
  `product_mtime` 的 decline 记录，且下一个 tick 重新评估它
- **THEN** `_already_ingested_runs` 把该 run 放进返回集（与 `retired` 并列的
  状态无关排除项），该 run 不进入 pending，没有新的 handoff 尝试发生，tick `rc == 0`
- **AND** 无论该 run 的 `hydro_run.status` 是 `published`、`parsed` 还是
  `succeeded`，抑制都同样生效——抑制 SHALL NOT 依赖于该 run 是否进入
  `status IN ('parsed','published')` 的完备性查询

#### Scenario: A never-published run is suppressed too

- **WHEN** 一个 `hydro_run.status = 'succeeded'` 的 run（从未 parsed/published，
  因而从不出现在完备性查询结果里）被压缩块守卫挡住并写入 decline 记录
- **THEN** 下一个 tick 该 run 同样不进入 pending，且没有新的 handoff 尝试发生

#### Scenario: A newer regeneration reopens the declined decision

- **WHEN** 一个已被 decline 的 run 的产物被重新生成，使 `product_mtime` 变新
  （或其 `init_state_id` 变更）
- **THEN** 已有的 decline 记录不再匹配，该 run 不再被并入 `_already_ingested_runs`
  的返回集，于是重新进入 pending 并被重试

#### Scenario: A guard-internal failure is not a compressed-chunk block

- **WHEN** `check_batch_targets_uncompressed` 因自身原因失败——catalog 查询超时
  （它给自己设了 5s `statement_timeout`）、批次窗口只有单端点、目标 hypertable
  未注册——从而抛出**基类** `CompressedChunkGuardError` 而非子类
  `CompressedChunkWriteError`
- **THEN** handoff SHALL 报告一个与真实压缩块阻塞**不同**的 reason code
  （`HANDOFF_APPLY_COMPRESSED_CHUNK_GUARD_FAILED`），`_process_run` 返回
  `outcome="failed"`，进程 `rc == 1`，**不**写入任何 decline 记录，该 run 继续重试
- **AND** `HANDOFF_APPLY_COMPRESSED_CHUNK_BLOCKED` SHALL 只由子类
  `CompressedChunkWriteError` 那条分支挂出，即它只表示"确实探测到压缩块"

#### Scenario: A decline record carries a diagnosable detail

- **WHEN** 一条 decline 记录被写入
- **THEN** 其 `detail` 列 SHALL 携带底层守卫消息（已经过 `redact_text`），
  而不是仅仅重复 reason code 本身——否则一条被误记的 decline 事后无法甄别

#### Scenario: A failed decline read degrades to no suppression, never to a crash

- **WHEN** 对 `ops.ingest_recompute_decline` 的读取失败（典型情形：代码已部署
  而迁移 `000055` 尚未 apply，或任何 `psycopg2.Error`）
- **THEN** 该读取 SHALL 被限定在自己的 savepoint 内，失败时降级为"不抑制任何
  run"，tick SHALL 正常完成并输出 JSON 汇总；抑制的缺失使被挡的 run 继续重试并
  以 `rc == 1` 报红——即退化为本变更之前的行为，而不是整个 tick 未捕获异常退出

#### Scenario: A genuinely unobtainable key fails closed

- **WHEN** 一次 `HANDOFF_APPLY_COMPRESSED_CHUNK_BLOCKED` 发生，但 `product_mtime`
  取不到，或 decline 记录写入抛出异常
- **THEN** `_process_run` 返回 `outcome="failed"`（不终态化），进程 `rc == 1`，
  该 run 在下个 tick 继续重试
- **AND** `init_state_id` 缺失**不属于**本场景（见 D11）：它是"已知无 manifest"
  而非"证据不可知"，按 `''` 记账并正常终态化。fail-closed 只剩上述两条真不可知路径

#### Scenario: Declined runs stay visible after the tick that declined them

- **WHEN** 任意 ingest tick 结束并输出 JSON 汇总
- **THEN** 汇总含 `declines_active` 字段，其值为 `ops.ingest_recompute_decline`
  的当前行数；读取失败时为 `null`（`null` 本身即"计数未知"的信号，绝不省略该字段
  也绝不因此把一次成功的 ingest tick 判红）。这使一条长期存在的终态记录在每个
  tick 上都可被 grep 到

#### Scenario: A declined run does not inflate ingested or publish counters

- **WHEN** 一个 tick 内既有 declined run 也有正常 ingested run
- **THEN** `runs.ingested` 只计入真正写入的 run，declined run 既不计入
  `runs.ingested` 也不计入 `runs.failed`，也不参与 `_stats_guard`（后者钉的是
  本 tick 真正 ingest 的条数，不是 publish 判据）

#### Scenario: A blocked run with no manifest still reaches the terminal state

- **WHEN** 一个被压缩块挡住的 run 既没有 manifest、`hydro_run.init_state_id`
  也为 `NULL`，但产物 mtime 可取
- **THEN** 它同样被 decline，记录的 `init_state_id` 为空串 `''`（合法键值，
  含义是"已知无 manifest"），并在其后的 tick 中被抑制、不再发起 handoff 尝试。
  fail-closed 只保留给真正不可知的情形：`product_mtime` 取不到、或 DB 写入失败
- **AND** 写入侧与读取侧必须由**同一个**键计算逻辑得出该键——否则会出现
  "写得进、读不出"的半修复：每 tick 重新 decline 被 `ON CONFLICT DO NOTHING` 吞掉，
  `rc` 变 0 而 handoff 仍在永久重试
- **AND** 日后若出现带真实 `initial_state_id` 的 manifest，键随之改变、与记录失配，
  该 run 自动重开重新评估（manifest 被瞬时读坏的情形同理自愈）

#### Scenario: A standing decline counts as already-done, exactly like a retired run

- **WHEN** 一条已存在的 decline 记录在**其后**的某个 tick 上仍与产物证据相符
- **THEN** 该 run 落在 `_already_ingested_runs` 的返回集里，因而计入
  `already_count` 并可独立满足 `publish_eligible`——这与 `retired`
  （`status='superseded'`）在 #1781 之前就有的行为**完全同形**，是刻意的并列语义，
  不是回归。此时 `_publish_display_runs` 的 UPDATE 命中零行（无 `parsed` 可推进），
  不写行、不动 `updated_at`。注意由此 `already_ingested` 字段会计入从未 ingest 过
  的 run；判断"本 tick 真正写了多少"一律看 `runs.ingested`，不要看该字段

#### Scenario: The decline lookup is batched and its object-store reads are bounded

- **WHEN** `_already_ingested_runs` 为一个 tick 的 run_ids 集合评估完备性
- **THEN** decline 记录通过单次 `WHERE run_id = ANY(...)` 查询一次性取回
- **AND** object store 的 manifest/mtime 读取只对**有 decline 记录的 run** 发生，
  次数与 decline 行数同阶，SHALL NOT 与 pending 规模同阶

#### Scenario: An unmatched decline key does not suppress

- **WHEN** 一个 run 有 decline 记录，但当前 manifest 的 `initial_state.state_id`
  或 `product_mtime` 取不到，或与记录中的值不相等
- **THEN** 该 run 不被抑制，正常进入 pending 并被重试
