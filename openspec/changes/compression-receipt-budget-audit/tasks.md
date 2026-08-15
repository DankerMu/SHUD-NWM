# Tasks: compression-receipt-budget-audit

## 1. Runner（scripts/node27_timeseries_compression.py）

- [x] 1.1 `budget` 落值：`build_receipt`/`build_refused_lock_receipt`/`build_failed_receipt`
      payload 加 `"budget": {"compress_timeout_ms": config.compress_timeout_ms, ...}` 三字段；
      `_replace_early_stale_with_failure` 不加（D2，签名不动）。
- [x] 1.2 版本升 "2.1"：`SCHEMA_VERSION` 常量 + :899/:992 两处硬编码，四发射点同版本。
- [x] 1.3 (d) 交叉不变量：`config_from_args` 第三腿（抬 timeout ⇒ bound 必须 1），
      `CompressionConfigError` 文案指向 runbook §4.5；置于既有两腿之后、构造之前。

## 2. Schema

- [x] 2.1 `schemas/timeseries_compression_receipt.schema.json`：`properties.budget`
      （additionalProperties:false，required 三字段，integer 下界与解析约束一致）；
      enum 加 "2.1"；failed 条件式 :104 放宽为 `enum ["2.0","2.1"]`；**:93 head_sha
      条件式同步放宽为 `enum ["2.0","2.1"]`（P1-1，防 2.1 丢 provenance 钉）**；
      D2 三段条件式（1.0/2.0 禁 budget；2.1 非豁免形状必有；豁免形状=failed +
      stage=="config" + 无 per_tick_bound，禁 budget）；编辑手段仅限
      if/then/else + required + not（P3-2）。
- [x] 2.2 `schemas/examples/timeseries_compression_receipt.example.json` 升 2.1 带 budget。

## 3. Tests

- [x] 3.1 `tests/test_node27_timeseries_compression.py`：
      (a) 非默认 env（1800000/1900/1940 + bound=1）→ 三构造点 receipt.budget 如实记录；
      (b) 默认 env → budget == 默认三元组；
      (c) tombstone 路径（config 错误 + 既有 stale receipt）产出无 budget 且 schema valid；
      配套 grep 钉：runner 源码 `stage="config"` 仅 tombstone 调用点（D2）；
      (d) schema 矩阵：半截 budget 拒、1.0/2.0 带 budget 拒、2.1 非豁免缺 budget 拒、
      2.0 无 budget 仍 valid（历史兼容正例）、2.1+per_tick_bound+stage=="config"+budget
      valid（非豁免假想形状不掉陷阱）；反例至少一条显式走 Draft7Validator（P3-2）；
      (e) (d) 腿红绿：抬墙+bound>1 拒（文案断言含 "§4.5"）、抬墙+bound=1 过、默认+bound=4 过、
      **env 模板字面量组合（timeout+bound 解析自 infra/env 模板）过 config_from_args（P2-3）**；
      (f) 既有 :126-384 与 :1570-1606 测试零修改通过；**既有三处字面适配（仅此三处）**：
      :1178-1184 1.0 降级测试同步 pop `budget`，:473 与 :1146 硬编码 "2.0" 升 "2.1"（P2-1/P3-1）。
- [x] 3.2 `tests/test_node27_timeseries_compression_live_evidence.py`：
      带 budget 的 2.1 receipt 过 **`_load_receipt` 结构校验路径**最小正例
      （**不进 verify_bundle**——其 :3564-3567 "2.0" 语义钉是 #1069 冻结 bundle 契约，
      保持不动，P1-2）；
      新测试钉两枚：`EXPECTED_TIMEOUT_SECONDS` 字面量 == 900 且定义行不引用 receipt/budget；
      `verify_bundle` 源码保留 `!= "2.0"` 语义钉（字面量断言）。

## 4. node-27 证据（零实机改动）

- [ ] 4.1 pre-merge：live env 非敏感 key grep（四个 key 中预期仅 `_PER_TICK_BOUND` 命中；
      三个预算 key **0 命中即是证据**——未设置 ⇒ 走代码默认 ⇒ (d) 腿不拒 live 组合）。
      2026-08-14 已实测：key 名单无预算三键。
- [ ] 4.2 pre-merge：分支代码（临时 detached worktree）D3 三步法产 scratch dry-run receipt，
      `--receipt-path` 与 lock 路径**双 scratch 覆写**（`--lock-path` flag 或 source 后
      re-export env——防撞生产锁：timer tick 撞上会向生产路径发 refused_lock receipt，P3-3）；
      断言含 `budget` 三字段 == 生效默认值、`schema_version == "2.1"`；
      **scratch receipt 用 jsonschema 对分支 schema 校验通过**（唯一真实数据的
      runner-output ⊨ schema 证据）；生产 receipt mtime 不动；凭据 grep 0 命中。

## 5. Docs

- [x] 5.1 runbook §4.5：四值表(:1578-1586)加"receipt 回执字段"列/行；Cleanup order
      (:1670-1694) 增补"从最新 receipt `budget` 确认已回默认（三值==默认三元组）"步骤。

## 6. Post-merge（orchestrator 收尾链义务，owner=orchestrator）

- [ ] 6.1 在 #1386 追评：spec :713 "receipt schema identical" 句因本变更第二次失真
      （2.1+budget），需随 #1386 的默认值修正一并改写（design Non-Goals P2-5 移交落地）；
      追评同时点名归档后同一 capability 文件内 :707 "840000 ms" 与新 requirement
      "默认值（3600000 ms）" 的数值并置矛盾（round-1 C2）。
- [ ] 6.2 round-1 B3 路由：历史 receipt 目录 glob 校验测试（3 行级）——merge 后交
      issue-scribe 或并入下一压缩车道 issue（deferred，当前四文件已手工验证 valid）。

## Evidence Floor

- `uv run pytest -q tests/test_node27_timeseries_compression.py tests/test_node27_timeseries_compression_live_evidence.py` 全绿
- 红绿证：承载条为**正例红证——新 runner 的 2.1+budget receipt 在旧 schema 下 FAIL**
  （P3-4：半截/1.0+budget 反例在旧 schema 因 additionalProperties:false 也拒，错因红，
  不作承载）；(d) 腿四组合绿 + 拒组合红
- `uv run ruff check .` ✅ · markdownlint ✅
- `openspec validate compression-receipt-budget-audit --strict --no-interactive` ✅
- node-27 4.1/4.2 证据贴 PR（scratch receipt 全文 + env grep 输出）
