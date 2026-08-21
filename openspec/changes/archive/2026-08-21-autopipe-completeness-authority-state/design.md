# Design（#1674）

## 风险分级

expanded：生产回归修复 + 判据语义变更（"published 即完整"），需真实 DB +
TimescaleDB 压缩块 fixture 与 node-27 live receipt；本地 mock 复现不出失效条件。

选用风险包：data-correctness（误判"完整"会让该重灌的 run 漏灌）、
spec-conformance（#1442 清零 oracle 与规格场景）、live-ops（node-27 tick receipt）。
未选：security（无边界变化）、performance（LEFT JOIN 对键可见 run 的代价与原
JOIN 同阶；NULL-key run 零匹配更便宜）。

## 必须保持

- `superseded` run 无条件退役（第一条查询不动）。
- 键可见 run 的 `parsed_at = MAX(rt.created_at)` 与 `_ingested_run_is_current`
  的重算检测语义不变（init_state 不一致 → 不跳过；产品 mtime 新于 parsed_at → 不跳过）。
- `_publish_display_runs` 的 SQL 逐字不动。
- `tests/test_river_ts_text_identity_cleanup.py::test_autopipeline_ingest_criterion_joins_by_key_with_no_aid`
  原样通过：`"ON rt.run_key = h.run_key" in sql` 仍成立、`rt` 上零文本列引用。
- 返回类型 `set[str]`、摄入汇总结构、rc 决策均不变。

## 决策

### D1 完整性 = 权威状态优先

```sql
SELECT h.run_id,
       h.init_state_id,
       MAX(rt.created_at) AS parsed_at
FROM hydro.hydro_run h
LEFT JOIN hydro.river_timeseries rt
  ON rt.run_key = h.run_key
WHERE h.run_id = ANY(%s)
  AND h.status IN ('parsed', 'published')
GROUP BY h.run_id, h.init_state_id, h.status
HAVING h.status = 'published' OR COUNT(rt.run_key) > 0
```

- `published` 由 `_publish_display_runs` 在行存在时翻转，是比"此刻键过滤能看见行"
  更强的权威事实；之后行不可见只有两种来源——NULL-key 遗留（契约排除）或
  retention 删 chunk（有意删除）——两者都不该触发重灌。
- `parsed` 仍要求键行：dual-write 之后 parsed run 必有键行；无键行的 parsed run
  说明 parser 链没写完，保持"未完整"让流水线重试是正确的。
- `parsed_at` 不做回退：遗留 NULL-key run 上 `MAX(rt.created_at)` 为 NULL，
  `_ingested_run_is_current` 的 mtime 比对因此跳过、只剩 init_state 比对。**不用**
  `h.updated_at` 兜底——publish 不碰它（`test_display_publish_status_only` 钉死），
  而每个风暴 tick 的 register upsert（`scripts/node27_ingest_run.py:205`
  `updated_at = now()`）已把这批 run 的 `updated_at` 推到最近一次 tick，拿它当
  parse 时刻是假精度。有界残留：7/22-7/30 遗留 cohort 上"同 init_state 的对象仓
  重算"不会被 mtime 检出，仅靠 init_state；该 cohort 随 retention 收敛，receipt
  记录其 run 数。
- 不做方案 (a)：`OR (rt.run_key IS NULL AND rt.run_id = h.run_id)` 被规格场景
  "经 join 到达的身份不得携带文本 fact join"明令禁止，清零 oracle 三处独立红，
  且重新耦合 #1342 删列；D1 不需要任何例外登记。

### D2 `_publish_display_runs` 不改

盲区存在但按契约收敛：遗留 NULL-key 人口只可能是 `published`（它们在 dual-write
之前就走完了发布）。2026-08-21T10:0xZ orchestrator 用 display RO DSN 实测
`SELECT status, count(*) FROM hydro.hydro_run GROUP BY 1` → published 3058 /
superseded 959 / parsed 0（receipt 在 #1674 评论）；tasks 0.1 实现前复测，若
`parsed > 0` 则逐条核对其行 `run_key` 是否全 NULL，为真即升级为本单阻塞项（需
另行裁定回放路径），不得静默。SQL 语句逐字不动；其 docstring 里"matching the
`_already_ingested_runs` completeness predicate"一句在 D1 后失真，允许只改
docstring（`test_publish_update_sets_status_only` 从 `cur.execute(` 起切片，
census 排除 docstring，均不受影响）。

### D3 测试形态

- 真实 DB（`pytest.mark.integration`，throwaway DB，node-27 oracle）：复用
  `tests/test_river_identity_normalization_integration.py` 的 authority 种子 +
  `_seed_facts(normalized=False)` + `compress_chunk` 惯用法，四个断言：
  (i) published + NULL-key 行在压缩 chunk → 在返回集（**修复前此断言红**，
  实现者先跑红再改）；(ii) parsed + NULL-key 行 → 不在；(iii) parsed + 键行 → 在；
  (iv) published + 零 fact 行 → 在。外加 (v) 重算检测：遗留 published run 的
  `object_store_root/runs/<run_id>/input/manifest.json` 的 initial_state 与
  `hydro_run.init_state_id` 不一致 → 不在；一致且只有产品 mtime 变新 → 仍在
  （钉住 D1 声明的有界残留，防止将来有人悄悄改语义而无人知）。
  种子事实（已核对 000006/000050）：`hydro.hydro_run` 最小列
  `run_id, run_type, scenario_id, model_id, basin_version_id, start_time, end_time, status, run_manifest_uri`，
  `run_key` 是 `GENERATED ALWAYS AS IDENTITY`，**不得**出现在 INSERT 列；FK 需
  `core.model_instance` / `core.basin_version`（`_seed_authority` 已覆盖），fact 行
  还需 `core.river_segment` / `core.river_network_version`；现有 `_seed_authority`
  / `_seed_facts` 硬编码单 run `'run1'`，多 run 多状态的 helper 是新代码。压缩
  对 SQL 语义不承重（NULL 键压不压都命不中），它是对 issue 失效条件的保真，
  测试叙述不得写成"压缩导致 (i) 红"。
  (vi)（round-1 审查 CONFIRMED 补齐）：键可见 published run + 一致 manifest，产品
  mtime 置为未来 → 不在（非 NULL `parsed_at` 确实流进 mtime 比对，钉住 must-preserve
  第二条）；mtime 置为过去 → 在。(i)-(v) 要么 `object_store_root=None` 要么
  `parsed_at` 为 NULL，都观察不到这条管道；旧内连接结构性保证非 NULL，LEFT JOIN 让
  NULL 成为合法输出，故必须单独钉。
- SQL 形态 pin：放在 `tests/test_river_ts_text_identity_cleanup.py`（已在
  selector 对 `scripts/node27_autopipeline.py` 的等值选择集内，新文件会让
  `test_select_ci_tests.py:412-421` 的等值断言红或根本不跑）：`LEFT JOIN`、
  `HAVING h.status = 'published' OR COUNT(rt.run_key) > 0`、`MAX(rt.created_at)`、
  无 `COALESCE`/`updated_at`；`_publish_display_runs` 的 `cur.execute(` 语句
  逐字等于 master。
- 既有 `tests/test_node27_autopipeline_handoff.py` / `_preflight.py` 全绿不改。

### D4 live receipt

node-27 `git pull --ff-only` 后等下一 tick（timer 背靠背触发）：
`already_ingested` 回到 2006（或当期全量）、`HANDOFF_APPLY_COMPRESSED_CHUNK_BLOCKED`
计数 ≤ 基线 12、`done rc=0`、elapsed ~240-250 s；连续 ≥2 tick 无
`status=1/FAILURE`。同时用 RO DSN 复测 `hydro_run` 状态分布（parsed 应仍为 0）。
