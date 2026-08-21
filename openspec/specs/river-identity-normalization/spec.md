# river-identity-normalization Specification

## Purpose
TBD - created by archiving change river-identity-normalization-backfill. Update Purpose after archive.
## Requirements
### Requirement: river_timeseries identity columns SHALL have integer surrogate-key targets with an idempotent, bounded, receipted backfill

The schema SHALL provide integer surrogate keys on the four existing
authority tables (`hydro.hydro_run`, `core.basin_version`,
`core.river_network_version`, `core.river_segment`), native enum types
for `variable`, `unit`, and `quality_flag`, and nullable normalized
columns on `hydro.river_timeseries` added without a table rewrite.
Backfill of existing rows SHALL run only through a bounded, receipted
runner that is dry-run by default, batches by ctid block ranges, is
resumable via a NULL-sentinel predicate plus a persisted block cursor,
enforces a per-batch transaction-duration wall, fails closed (with
distinguishable receipt counters) on authority-unmatched or
enum-unmappable values, and never issues DML against compressed chunks
or the active (currently ingested) chunk. The production compression
settings and primary key SHALL remain unchanged by this change;
switching both is delivered as a read-only verify function plus a
fail-closed cutover function that the migration chain never invokes.

The runner's stop/receipt observability SHALL additionally distinguish
lock contention from slowness and keep totals honest about unmeasured
chunks: SQLSTATE 55P03 (lock_not_available) and 40P01
(deadlock_detected) during a batch SHALL stop the run under a dedicated
stop stage `lock_contention` (no halved-range retry, remediation advice
distinct from `duration_wall`); a shortfall stop whose diagnostic counts
are both zero (`unmatched_rows == 0` and `unmappable_rows == 0`) SHALL
name the concurrent-DELETE double-snapshot signature in its reason so
operators re-check the parser re-parse window before escalating as data
corruption; and `totals.pending_rows` SHALL be documented as summing
only chunks measured in the invocation (a skipped chunk contributes
nothing, so 0 does not assert the table is sentinel-free).

#### Scenario: Migration replays idempotently without rewriting the fact table

- **WHEN** the migration chain through 000050 is applied twice to an
  empty database
- **THEN** both passes succeed, the four surrogate-key columns, three
  enum types, and seven nullable fact columns exist exactly once, and
  `pg_attribute.atthasmissing` is false for all seven new fact columns
  (no stored default, no rewrite)

#### Scenario: Backfill is re-entrant and bounded

- **WHEN** the backfill runner is interrupted after some batches and
  re-invoked
- **THEN** it resumes without re-updating rows whose sentinel key is
  already set (block cursor loss degrades to a full rescan with
  identical results), a second complete pass reports zero changed rows,
  every batch runs inside its own transaction bounded by the configured
  duration wall with one halved-range retry, and each invocation emits
  a schema-validated receipt

#### Scenario: Unresolvable values, compressed chunks, and the active chunk fail safe

- **WHEN** the runner detects a per-batch shortfall between sentinel
  candidates and updated rows (authority-unmatched or enum-unmappable
  values), or encounters a chunk reported compressed by the shared
  write-guard's chunk assertion, or the active chunk
- **THEN** the shortfall stops the run fail-closed with distinguishable
  receipt counters (never silently left as progress), compressed and
  active chunks receive no UPDATE and are listed in the receipt as
  skipped, and the documented recovery paths are the existing
  decompression-replay/compression runners (compressed) and either a
  later catch-up round once the chunk is terminal or an explicit
  final-sweep invocation that first asserts ingest is quiescent
  (active)

#### Scenario: Cutover is a fail-closed single-transaction window operation, never auto-applied

- **WHEN** `hydro.cutover_river_identity_normalization()` is invoked
  with any compressed chunk present, or with any NULL remaining in the
  seven normalized columns
- **THEN** it raises an error and changes nothing (a compressed chunk
  fails the explicit precondition; a NULL fails the in-transaction
  VALIDATE CONSTRAINT step, rolling everything back); only inside a
  maintenance window — ingest paused, final sweep done, read-only
  verify counts at zero, all chunks decompressed — does it execute the
  measured working sequence in one transaction: disable compression,
  drop the text foreign key (measured TimescaleDB 2.10 rule: foreign
  key columns must be covered by segmentby∪orderby, so the text FK
  cannot survive integer segmentby), validate NOT NULL via check
  constraints then set the seven columns NOT NULL scan-free, replace
  the primary key with the integer/enum form (in-window index build),
  and re-enable compression with segmentby/orderby on the normalized
  columns (TimescaleDB 2.10 requires segmentby∪orderby to cover the
  primary-key columns, so the two switches are inseparable), after
  which a compress/decompress round-trip preserves row data; the
  migration chain itself never calls the verify or cutover functions,
  keeping CI and production schemas convergent

#### Scenario: Double-zero shortfall names the concurrent-DELETE signature

- **WHEN** a batch stops with `shortfall > 0` while both
  `unmatched_rows` and `unmappable_rows` are zero
- **THEN** the stop remains fail-closed under stage `shortfall` with
  unchanged rollback and cursor-rewind behavior, and the stop reason
  directs the operator to check for a concurrent DELETE (parser
  re-parse window) between the candidate count and the UPDATE before
  treating the stop as referential rot or enum overflow

#### Scenario: Lock contention stops under its own stage, not duration_wall

- **WHEN** the batch UPDATE fails with SQLSTATE 55P03 or 40P01
- **THEN** the run stops fail-closed under stage `lock_contention`
  without a halved-range retry, the reason carries the SQLSTATE and
  advises pausing the ingest writer / waiting for an idle window (with
  the final-sweep quiescence gate noted as enforcing that pause on the
  active chunk only) rather than tuning batch size or the duration wall,
  the receipt validates against the schema, and the statement-timeout
  (57014) path keeps its existing halved-retry-then-`duration_wall`
  behavior unchanged

#### Scenario: totals.pending_rows covers only measured chunks

- **WHEN** every eligible chunk in an invocation reaches zero pending
  rows while at least one chunk was skipped as compressed or active
- **THEN** the receipt may legally report `totals.pending_rows == 0`
  with the skipped chunk's per-chunk `pending_rows` null and the
  `chunks_skipped_*` counters non-zero, and the schema documents that
  this total covers only measured chunks rather than asserting the
  whole table is sentinel-free

### Requirement: river_timeseries writers SHALL dual-write surrogate identity columns atomically with the text columns

Writers SHALL populate surrogate identity in the same statement as text:
every production or seed writer that INSERTs into `hydro.river_timeseries`
populates the seven normalized columns (`run_key`,
`river_network_version_key`, `basin_version_key`, `river_segment_key`,
`variable_e`, `unit_e`, `quality_flag_e`) in the same INSERT statement
that writes the legacy text columns, leaving the text-column write
byte-identical to the pre-change behavior. Surrogate keys SHALL be
resolved by read-only SELECTs against the four authority tables — the
dual-write path never creates authority rows (pre-existing seed
authority inserts are out of scope); for the production parser this
means extending its existing context/segment load queries with zero
additional round-trips. An unresolvable identity value
SHALL fail the whole batch closed with a structured error — a NULL
surrogate on a newly written row is never a legal outcome. Enum columns
SHALL receive the same in-process text value as their text counterparts,
coerced server-side by the enum column type, so text↔enum divergence is
unrepresentable in the writer and out-of-vocabulary values fail closed.
Conflict-update branches SHALL
mirror the text columns they refresh: every text column in the
`ON CONFLICT ... DO UPDATE SET` list has its surrogate counterpart set
from `EXCLUDED`, and identity (conflict-target) columns are re-set on
neither side.

#### Scenario: New rows carry a complete, consistent surrogate identity

- **WHEN** a parse run writes rows through the dual-write path into a
  database carrying migration 000050
- **THEN** every newly written row has all seven normalized columns
  non-NULL, the read-only verify function reports zero equality-audit
  divergence for those rows, and the legacy text columns are populated
  exactly as before the change

#### Scenario: Conflict updates cannot re-introduce text↔surrogate drift

- **WHEN** the writer's INSERT statement is replayed directly against an
  existing row whose identity matches but whose refreshable text columns
  (`basin_version_id`, `unit`, `quality_flag`) have drifted, so the
  `ON CONFLICT DO UPDATE` branch fires (the production DELETE-replace
  path removes conflicting rows first — this branch is the safety net
  for concurrent or replayed writes, and is exercised by replaying the
  statement without the preceding DELETE)
- **THEN** the surviving row's `basin_version_key`, `unit_e`, and
  `quality_flag_e` reflect the same update as their text counterparts,
  and the identity columns (text and surrogate) are unchanged

#### Scenario: Unresolvable identity or out-of-vocabulary value fails the batch closed

- **WHEN** a row references an identity value absent from its authority
  table, or carries a variable/unit/quality-flag literal outside the
  corresponding enum's value set
- **THEN** the batch write raises a structured error and no rows from
  that batch are persisted — the writer never falls back to writing
  NULL surrogates on new rows

#### Scenario: Dual-write coexists with the #1339 backfill lane

- **WHEN** dual-written rows and legacy text-only rows coexist in the
  table and the identity backfill runner executes
- **THEN** only legacy rows (NULL sentinel) are counted as backfill
  candidates, dual-written rows are not re-updated, and rolling the
  writer back to the pre-change code merely produces new sentinel rows
  that the existing backfill lane converges later — no data loss in
  either direction

### Requirement: in-boundary river_timeseries readers SHALL filter by surrogate keys with field-identical external responses

Display-boundary readers of `hydro.river_timeseries` SHALL filter by the surrogate key and enum columns as the row-selection authority, and SHALL additionally retain redundant text pushdown predicates on exactly `run_id`, `river_network_version_id`, and `variable` — each conjoined (AND) with its key or enum counterpart — in every fact query whose plan can reach compressed chunks, as declared transitional aids for compressed-chunk `segmentby`/`orderby` predicate pushdown while compression settings remain text-based (user-adjudicated remedy, issue #1341 comment thread; removed together with the text-column drop in #1342, where any missed removal fails loudly because the columns are gone). These pushdown predicates are strict no-ops for key-carrying rows and MUST NOT widen results: NULL-key rows stay excluded by the key predicates. No other text column may appear as a fact predicate, with one positional exception below. The aids apply where the identity arrives as a bound literal; identity that reaches the fact table through an authority-table join stays key-joined only — text-column fact joins remain forbidden outside the sanctioned probe bodies — so such query legs carry only the aids whose identity is bound (typically `variable` alone).
Round-3 amendment (P1 EXPLAIN-gate interception, PR #1443: the set-based national legs lost the per-segment probe path and regressed 0.77s→34.7s), extended by #1596 (the set-based `source_identity_stats` existence probe fully decompressed compressed chunks — 23-37s per probe, and 38s for an empty tile on uncovered compressed instants): inside the three `hydro-national` `CROSS JOIN LATERAL` probe bodies in `services/tiles/mvt.py` — the two per-segment data-leg probes and the per-identity existence probe of `source_identity_stats`, and only there — correlated text equalities are sanctioned as the same class of transitional pushdown aids: `run_id`, `river_network_version_id`, and `river_segment_id` in the data-leg probe bodies, and `run_id` and `river_network_version_id` only in the identity-existence probe body (it has no per-segment correlation). Each is conjoined (AND) with its surrogate-key counterpart in the same probe, each is a strict no-op for key-carrying rows (all are NOT NULL primary-key columns), and all are removed together with the text-column drop in #1342. The identity-existence probe's #1342 survival is split by chunk state: the cutover layout mirrors the text layout column-for-column (surrogate primary key `run_key, river_network_version_key, river_segment_key, variable_e, valid_time`; `compress_segmentby` on the first three key columns — migration 000050), so after the aids are removed with the text columns the probe binds the same positional subset (PK positions 1, 2, 4, 5; segmentby 2 of 3) through its surrogate-key predicates — on compressed chunks its segmentby plan therefore survives unchanged; on uncompressed chunks its pre-cutover index pick was measured, not presumed (PR #1657 E4 receipt): the text primary key's run-scoped prefix on the hit branch, and the retained single-column `river_timeseries_valid_time_idx` on the interior-gap miss branch — neither of which #1342 removes, since the former mirrors onto the surrogate primary key at the same positions and the latter indexes the time dimension the cutover does not touch; the post-cutover index set is owned by #1342. This positionally widens the user-adjudicated three-column literal-aid set for the lateral probe bodies only — each widening recorded as a deviation in the PR 偏离记录 for user review, since the three-column set was a user-adjudicated remedy. Outside a lateral probe body the prohibition on text-column fact joins stands unchanged, and the shape oracle (`LATERAL_PROBE_TEXT_PUSHDOWN_COLUMNS` vs `FORBIDDEN_TEXT_FACT_COLUMNS`) enforces exactly this positional split.
The `source_identity_stats` existence probe for `hydro-national` SHALL locate candidate identities through the same display-coverage-gated discovery shape as `latest_runs` (a `hydro.run_display_coverage` window filter joined through the run/network authority tables, selecting the surrogate keys alongside their text identities) and SHALL verify per-instant existence by touching `hydro.river_timeseries` per identity — answering existence from the coverage window alone is forbidden: the window is a MIN/MAX over complete instants, not a per-instant bitmap, so a coverage-only answer can flip the no-data branch (HTTP 424) into an empty-tile 200 on interior window gaps. The probe's zero branch (no display-ready run, or a covered window whose instant has no fact rows) SHALL remain byte-identical to the pre-change behavior.
This covers `services/tiles/mvt.py`,
`packages/common/display_coverage.py`, and
`apps/api/routes/hydro_display.py`. It also governs any future
identity-predicated fact query under `services/production_closure/`; that set is
empty at delivery time — the directory's `river_timeseries` references are
table-level deny-write probes, an evidence-token string, and one static plan
fixture, none of which carry an identity predicate (per-file disposition in
design.md). The requirement is: resolving caller-supplied text
identity through the four authority tables and restoring text output
via authority joins or enum-to-text casts, so that external responses
remain field-identical to the text-predicate era: JSON responses
byte-identical, MVT tiles equal as decoded feature sets, `feature_id`
concatenation byte-identical, and any ordering over identity columns
expressed on the restored text values. An unknown identity value or an
out-of-vocabulary enum literal SHALL yield the same empty result the
text predicates produced, never a SQL error. The switched read shapes
SHALL be served by an integer discovery index on `(run_key,
basin_version_key, river_network_version_key, variable_e, valid_time
DESC)` added by migration without dropping any existing text index;
text columns and text indexes remain authoritative for rollback and
for out-of-boundary readers until their separately delivered
retirement. Legacy rows whose surrogate keys remain NULL (only rows
outside the receipted backfill scope, i.e. compressed chunks pending
retention) are invisible to key-filtered reads; this exclusion is an
explicit, recorded contract with a bounded convergence deadline, not
silent data loss.

#### Scenario: Switched reads are field-identical for resolvable identities

- **WHEN** the same display request (tile, valid_times, coverage, or
  existence probe) is issued for an identity whose rows all carry
  surrogate keys, before and after the read-path switch
- **THEN** JSON responses are byte-identical, MVT tiles decode to equal
  feature sets (all properties including the `feature_id`
  concatenation, and geometry), and response ordering is unchanged

#### Scenario: Unknown or out-of-vocabulary identity degrades to empty, not error

- **WHEN** a switched query binds a `run_id` absent from
  `hydro.hydro_run` or a `variable` literal outside
  `hydro.river_variable`
- **THEN** the query returns the empty result the text predicates
  returned, and no enum-cast or other SQL error escapes to the caller

#### Scenario: NULL-key legacy rows are excluded as a recorded, converging contract

- **WHEN** rows with NULL surrogate keys exist in compressed chunks
  that the backfill runner cannot update
- **THEN** key-filtered reads exclude those rows, the exclusion scope
  (chunk ranges, row counts, retention deadline) is recorded in the
  delivery evidence, and no in-boundary reader re-admits NULL-key rows
  through text predicates: the sanctioned transitional pushdown
  predicates are conjunctive and can only narrow, never widen, the
  key-filtered result

#### Scenario: Transitional text pushdown predicates are bounded to the sanctioned set and paired with keys

- **WHEN** an in-boundary fact query contains a text identity predicate
- **THEN** that predicate is on `run_id`, `river_network_version_id`,
  or `variable` only, appears in the same conjunction as its surrogate
  key or enum counterpart, and no text predicate on
  `basin_version_id` or `river_segment_id` (nor any text-column join
  into the fact table) exists in any in-boundary read shape — except
  inside the three `hydro-national` `CROSS JOIN LATERAL` probe bodies,
  where the amendments above additionally sanction correlated
  text equalities (`run_id`, `river_network_version_id`, and
  `river_segment_id` in the data-leg bodies; `run_id` and
  `river_network_version_id` in the identity-existence probe body),
  each key-paired, removed with #1342; no `ts.`
  fact reference may appear outside those probe bodies in the national
  legs

#### Scenario: Switched shapes are served by the integer index without text-read regression

- **WHEN** the switched query shapes run on the production-scale
  database after the integer discovery index is applied
- **THEN** `EXPLAIN (ANALYZE, BUFFERS)` shows them planned on the
  integer index with no sequential scan of `hydro.river_timeseries`
  and latency no worse than the text-index baseline, while retained
  text indexes keep serving out-of-boundary text readers unchanged;
  shape carve-out (round 3, extended by #1596): the three
  `hydro-national` lateral probe legs instead plan as per-segment (data
  legs) or per-identity (existence probe) parameterized probes — on
  compressed chunks all three probe through the compressed `segmentby`
  index; on uncompressed chunks the data legs plan on the text primary
  key (measured in PR #1443) while the identity probe's pick is
  recorded, not presumed, by the delivery receipt — measured in PR
  #1657 as the text primary key's run-scoped prefix on the hit branch
  and the retained single-column `river_timeseries_valid_time_idx` on
  the interior-gap miss branch, with run / network / variable falling
  to filters;
  the integer index remains the planned path for every other switched
  shape, and #1342 owns the post-cutover index set that replaces the
  text plans for these legs

#### Scenario: Compressed-chunk portions keep predicate pushdown via the transitional text predicates

- **WHEN** a switched query shape whose plan reaches a compressed chunk
  (text-based `segmentby`/`orderby` settings still in force) runs with
  the transitional pushdown predicates present
- **THEN** the compressed-chunk portion of the plan shows an index or
  filter condition on the compression-internal relation driven by the
  text `segmentby`/`orderby` columns, not a full-decompression
  sequential scan over all batches

#### Scenario: The national existence probe answers interior coverage-window gaps with the no-data branch

- **WHEN** a display-ready run's coverage window covers the requested
  valid_time but `hydro.river_timeseries` holds no rows for that exact
  instant (an interior window gap), and the `hydro-national` tile is
  requested
- **THEN** `source_identity_count` is 0 and the endpoint returns the
  same HTTP 424 `MVT_LIVE_POSTGIS_UNAVAILABLE` the pre-change probe
  produced — the probe touches the fact table per identity and never
  answers existence from the coverage window alone

#### Scenario: The national existence probe stays sub-second on compressed instants in both branches

- **WHEN** the `hydro-national` tile is requested for a valid_time
  pinned inside a compressed chunk, once for an instant with a
  display-ready covered run and once for an instant with none
- **THEN** the identity-existence probe plans as per-identity
  parameterized probes: no full-decompression sequential scan over all
  batches, the fact-side inner node's loop count equals the number of
  identities probed before short-circuit (leading misses plus one when
  the covered instant has rows; every candidate when the covered
  instant is an interior gap; the uncovered request finds no candidates
  and never touches the fact table), and shared buffer touches on the
  compressed-chunk relations do not exceed the pre-change shape's on
  the same instant in the same session; a covered instant with rows
  serves the same tile bytes as before the change, a covered interior
  gap returns the no-data branch of the preceding scenario, and the
  uncovered request returns its empty response in under one second

### Requirement: Out-of-boundary river_timeseries consumers SHALL filter and emit identity by surrogate keys with per-group sanctioned transitional aids

`packages/common/forecast_store.py`（九处查询块，含 A9 fallback 的整条 CTE 链）、`services/tile_publisher/publisher.py`、`services/tile_publisher/forcing_copyback_backfill.py`、`scripts/node27_autopipeline.py`（ingest 判据与 publish 回填 EXISTS）、`db/seeds/seed_demo.py`、`scripts/summarize_qhh_smoke_results.py`、`scripts/reset_qhh_smoke_db.py`、`tests/integration_helpers.py` 对 `hydro.river_timeseries` 的过滤、连接、聚合**与身份输出**（SELECT/GROUP BY 中的文本身份列）MUST 以代理键（`run_key`/`basin_version_key`/`river_network_version_key`/`river_segment_key`）与枚举（`variable_e`/`unit_e`/`quality_flag_e`）为主形态；对外仍需文本处 MUST 从权威表 join 还原或枚举 `::text` 还原，payload 对**键收敛行**逐字段等价（NULL-key 遗留行对键过滤不可见——继承本 capability 已记录的有期限排除契约，不视为数据丢失）。

文本谓词 MUST 限于受批过渡下推辅助列（`run_id`/`river_network_version_id`/`variable`，以及 **A 段块绑定字面量形态下的 `river_segment_id`**——它是压缩 segmentby 第三列，缺席即压缩腿整 network 解压（node-27 E4(ii) EXPLAIN receipt 为证；未压缩腿由 000051 键索引 + 堆过滤承担，辅助对其无收益，键形后继索引随 #1342 交付）），且仅当 (a) 该身份以字面量/绑定参数形态出现（经权威表 join 到达的身份保持 key-join only，文本 fact join 在受批探针体外禁止）且 (b) 查询可达压缩 chunk 时保留，带 `remove with #1342` 标记，并与其键/枚举对应物同一合取式；`basin_version_id` 文本谓词与非 A 段块的 `river_segment_id` 文本谓词 MUST 清零。生产（PostgreSQL）路径的 segment 计数 MUST 用键的行元组 DISTINCT；`publisher.py` 的 sqlite 测试路径 MUST 用键基的方言等价构造（整型拼接计数、直取枚举列），语义一致。

清零 MUST 由 `tests/test_river_ts_text_identity_cleanup.py` 看护：别名限定面用渲染 SQL 断言（复用 `tests/test_sql_shape_helpers.py` 机制），裸列/片段面用逐调用点定向断言；范围仅本单在册文件（display 面由既有 oracle 看护；`db/migrations/**` 与 `scripts/node27_river_identity_backfill.py` 按定义读文本列，不在册）。oracle 看护 MUST 满足三个接线维度：(1) 每个受批文本辅助 MUST 与其键/枚举对应物出现在同一合取式中（辅助单独存活即 oracle 红），`remove with #1342` 标记 MUST 与辅助行相邻；(2) 在册文件内新增的 `hydro.river_timeseries` 语句 MUST 强制 register 更新（普查断言，新语句未入册即红）；(3) `scripts/select_ci_tests.py` MUST 让任一被守护生产文件的 diff 选中本 oracle（沿 #1341 at-site 规则惯例）。

#### Scenario: 曲线端点响应对键收敛 run 逐字段等价

- **GIVEN** 一个键收敛的 run（未压缩 chunk 七键 NULL 计数为 0 的 preflight 已过）
  在切换前后各取一次预报曲线端点响应
- **WHEN** 逐字段 diff
- **THEN** 全部字段相等，`unit` 字段非空且与切换前相同

#### Scenario: publisher 发布发现计数与 layer_id 不变

- **GIVEN** 同一批键收敛 run 的 q_down 发布发现聚合
- **WHEN** 以键元组计数替代文本拼接计数、以键分组 + 权威表还原替代文本分组
- **THEN** `segment_count` 与切换前一致，`layer_id` 拼装值不变

#### Scenario: 经 join 到达的身份不得携带文本 fact join

- **GIVEN** 任一在册文件把 `rt.run_id = h.run_id` 类文本等值加进事实表 join
- **WHEN** 运行清零 oracle
- **THEN** 测试失败（该形态不属于受批辅助——辅助只允许字面量/绑定参数绑定）

#### Scenario: 禁列谓词与无标记辅助被 oracle 拒绝

- **GIVEN** 在册文件的渲染 SQL 中出现 `basin_version_id` 文本谓词（或 A 段块
  受批形态之外的 `river_segment_id` 文本谓词），或受批辅助行缺
  `remove with #1342` 标记
- **WHEN** 运行 `tests/test_river_ts_text_identity_cleanup.py`
- **THEN** 测试失败并指出调用点

#### Scenario: 失去键伴随的辅助被 oracle 拒绝

- **GIVEN** 在册文件的某条渲染 SQL 中，受批文本辅助（如 `rt.variable =
  'q_down'`）仍在，而其同合取式的键/枚举对应物（如 `rt.variable_e`）被删除
- **WHEN** 运行清零 oracle
- **THEN** 测试失败（#1342 删列后该辅助将静默失去过滤或直接报错，二者都不可接受）

#### Scenario: 在册文件新增文本身份语句被普查抓住

- **GIVEN** 向在册文件新增一条含 `hydro.river_timeseries` 文本身份谓词的语句
  而不更新 register
- **WHEN** 运行清零 oracle 的普查断言
- **THEN** 测试失败并指出该文件的语句清单已过期

#### Scenario: 裸列面同受看护

- **GIVEN** `scripts/reset_qhh_smoke_db.py` 的 `_delete()` WHERE 片段或
  `tests/integration_helpers.py` 的 IN 谓词被改回文本身份列
- **WHEN** 运行清零 oracle 的定向断言
- **THEN** 测试失败

### Requirement: The parser's river_timeseries replace chain SHALL locate rows by surrogate keys end to end

`workers/output_parser/parser.py` 对 `hydro.river_timeseries` 的**全部三处**文本谓词——存在性探针（:840-845）、`WITH existing AS MATERIALIZED` 取窗（:853-859）、replace DELETE（:890-897）——MUST 以 `run_key`/`river_network_version_key`/`variable_e` 定位（无文本辅助：`check_batch_targets_uncompressed` 保证目标未压缩，辅助无下推收益且 DELETE 侧只会收窄漏删面），valid_time 窗谓词不变；replace 语义（同 run + 同 network + 同 variable + 窗内先删后插的幂等重放）与取窗对压缩守卫判据的输入 MUST 保持。合并前 MUST 有 node-27 键收敛 preflight receipt（未压缩 chunk 七键 NULL 计数为 0）。`tests/test_timescale_write_guard_wired.py` 的 DELETE 参数断言 MUST 与新谓词形状一致重钉。

#### Scenario: 同窗重放幂等

- **GIVEN** 同一 run 的同一 valid_time 窗被 parser 重放（preflight 已证键收敛）
- **WHEN** replace 链按键定位执行
- **THEN** 取窗结果与文本定位一致，窗内旧行删净、新行插入，行数与重放前一致

#### Scenario: 三处谓词无一遗漏

- **GIVEN** parser 模块源中的三条 `hydro.river_timeseries` 语句
- **WHEN** 运行清零 oracle 对 parser 的断言
- **THEN** 探针、取窗、DELETE 三处均为键谓词，无文本身份列

#### Scenario: 写守卫参数形状被钉住

- **GIVEN** DELETE 语句的参数元组
- **WHEN** 运行 `tests/test_timescale_write_guard_wired.py`
- **THEN** 断言的参数形状与实现一致（键 + 窗界，无文本列）

