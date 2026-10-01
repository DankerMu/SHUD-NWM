# Tasks — basin-catalog-semantics-and-rename-leftovers

Fixture level：expanded。风险包：
- data-integrity（生产删行）
- external-contract（公网 `/api/v1/basins` 语义不变）
- ops-runbook

## Must preserve

- `GET /api/v1/basins` 与 `/versions` 的行为、响应字段和分页完全不变，`list_basins` 不改。
- 已退役流域（A 组 hhe、zhaochen_*，C/D 组 5 个）的 `core.basin` 行保留，不进入删除工具。
- 删除脚本只处理 7 个点名的改名残影；默认 ROLLBACK（dry-run）；有 fsync 备份和 rollback 脚本。
- 退役流域（包括没有任何业务数据的 `basins_neiliuqu`）一律保留 `core.basin` 行。
- node-22 不写，只允许只读 `grep` 核查。

## 1. #2621 判据与巡检

- [x] 1.1 新增 `scripts/basin_catalog_manifest_audit.py`（D1）。
- [x] 1.2 单测：
  - 通过；
  - 存在 active extra 时判违规；
  - display 与 manifest 双向漂移；
  - manifest 缺失、不可解析或为空时 exit 2；
  - 分页取满（集合大于单页）；
  - receipt 字段齐全。
- [x] 1.3 真实 PG 集成测试（`NHMS_RUN_INTEGRATION=1`，throwaway DB）：种子流域分三类：
  - display 且在 manifest；
  - 退役（没有 active 模型、不在 manifest）；
  - active 但不在 manifest。

  断言 exit 码与 receipt。
- [x] 1.4 selector：新脚本路由到新测试（`scripts/select_ci_tests.py` + `tests/test_select_ci_tests.py`，满足 meta-guard）。

## 2. #2621 改名残影删除脚本（D2，沿用 #1729 oneshot 模式）

- [x] 2.1 新增 `scripts/ops/node27_2621_delete_rename_leftovers.sql`：
  - 常量、successor 与盘点计数写死在脚本里；
  - FK 清单等值检查、trigger 检查、ID 集合与零依赖检查；
  - `FOR UPDATE` 锁定；
  - 用 `= ANY(数组)` 做 hypertable 零引用探测，并计时；
  - `@copy-out` 备份；
  - 按顺序删除，`river_segment` 和 `met_station` 两步用 replica 模式，之后立即切回 origin 并断言；
  - Gate B 孤儿检查。
- [x] 2.2 新增 `scripts/ops/node27_2621_delete_rename_leftovers_rollback.sql`：
  - 父到子的顺序执行 `@copy-in`；
  - 保留 identity 列的原值；
  - 目标行仍存在时拒绝恢复；
  - 外键出边集合漂移、或表外父行（`met.canonical_grid_snapshot`）缺失时，在任何 COPY 之前拒绝恢复。
- [x] 2.3 disposable-DB 集成测试（`NHMS_RUN_INTEGRATION=1`，仿照 `tests/test_node27_1729_evidence_basin_delete_integration.py`）。按真实 id 和真实计数种子，river_segment、crosswalk、met_station 用 `generate_series` 生成，不允许任何覆盖计数的路径。覆盖以下情况：
  - apply 之后 rollback，恢复结果逐字节相等；
  - 没有 copy-dir 时拒绝执行；dry-run 的 copy-dir 不能复用；
  - 以下每种情况各一例，都应拒绝且不删任何行：计数漂移、存在 hydro_run、存在 interp_weight（按目标站点、按目标模型各一例）、存在 forcing_version、目标站点 grid snapshot 分布漂移、存在 active 模型、在 manifest 中、successor 缺失、存在 hypertable 引用、FK 集合漂移；
  - 无法设置 replica 时直接失败，不退回慢路径；
  - rollback 拒绝覆盖仍然存在的行；grid snapshot 父行缺失或外键出边集合漂移时，在任何 COPY 之前拒绝，不做部分恢复。
- [x] 2.4 `tests/test_node27_oneshot_sql.py`：两个新脚本的列集合守卫必须逐字节一致。selector 路由也要补上。

## 3. 生产执行（node-27）

- [ ] 3.1 处置前 receipt：
  - audit（预期 exit 0，16 个 extra，active 均为 0）；
  - 公网默认、display、manifest 三方集合落盘；
  - 只读 `grep` node-22 本地注册表副本，看补登的 5 个流域是否仍在其中（不写 node-22）。
- [ ] 3.2 停写入 timer（node-27 没有 TimescaleDB 压缩/保留策略作业，只有 user systemd timer）：
  1. `systemctl --user list-timers --all --no-pager` 落 receipt；
  2. `systemctl --user stop nhms-node27-timeseries-compression.timer nhms-node27-timeseries-retention.timer nhms-node27-autopipe.timer`；
  3. `systemctl --user is-active` 对应三个 `.service` 都是 `inactive`（在跑就等，不 kill）；
  4. 通过 runner 跑删除 dry-run（新的 `--copy-dir`，`--set nhms.manifest_basins=…`），NOTICE、各步耗时和 hypertable 探测耗时都作为 receipt 落盘；
  5. `systemctl --user start` 这三个 timer，再记一次 `list-timers`。
- [ ] 3.3 **停下来请 owner 确认** dry-run receipt。确认后按顺序执行：
  1. 同 3.2 的 1–3：记 `list-timers`、停三个 timer、确认三个 service `inactive`；
  2. 用新的 `--copy-dir` 执行 `--apply`，`--set nhms.probe_timeout_s` 取 dry-run 实测最大值 ×2；
  3. `systemctl --user start` 三个 timer；
  4. 启动后的 `list-timers` 记入 receipt；
  5. 把 copy 目录复制一份离开 node-27。
- 如果 dry-run 的探测超时，停下报告 owner，不自行放宽。
- [ ] 3.4 处置后 receipt（删前也记一份同口径的，逐项对比）：
  - audit（预期 exit 0，9 个 extra）；
  - 公网默认 57、display 48、manifest 48；默认 57 要在 display API 目录缓存（`apps/api/display_cache.py`，TTL 60 s、stale 最长 600 s）过期后读：隔 ≥ 60 s 读两次，读到 57 再记；
  - `/api/v1/basins/<删除项>/versions` 返回 404；
  - `GET /api/v1/layers` 中 `river-network` / `discharge` 的 `metadata.source_generation`（`national_river_network_source_version` / `national_discharge_source_version`）与 `cache_version` 删前删后不变；
  - seed 源目录中没有 `DNZH-*`/`xinan*`；
  - `object-store/runs/` 下目标 run 目录数（2026-09-29 实测 14 个 `fcst_{gfs,ifs}_2026091700_dg_<目标 dg 模型>`）与 `scheduler/state-index/index-last.json` 中目标模型条目数（实测 14 条，均 usable）删前删后不变；
  - `AUTOPIPE_EXCLUDE_BASINS` 未改动：与 `infra/env/node27-ingest.env.bak-onboarding19-rename-20260922` 的 diff 删前删后一致，恰好是追加的 7 个 id（它们挡住上面的 run 目录，必须保留，不是死配置）。

## 4. 文档

- [x] 4.1 `docs/runbooks/production-ops/operating-scope.md` §7：
  - 默认 `/basins` 口径与判据（引用 audit 脚本）；
  - 「退役行保留」与「改名残影可按门禁删除」的区分；
  - 补登 §7.x：2026-08-25 `neiliuqu` 退役；2026-09-22 onboarding-19（SHJ 三子流域并入 `basins_shj`、`xinanjiang_upstream` 退出，published run 已 superseded，`AUTOPIPE_EXCLUDE_BASINS`、`Basins-retired/onboarding19-20260922`、manifest `.pre-onboarding-19-*` 备份）；2026-09-29 改名残影删除记录。
- [x] 4.2 `docs/runbooks/production-ops/service-bringup.md:650-690` 的 baseline 清单标为历史快照。
- [ ] 4.3 #1729 追加评论：第 3 条验收项的口径统一。
- [x] 4.4 #2644：`openspec/glossary.md` 新增 `model_id` 词条，内容包括：
  - 基础包 id `<basin>_shud`，来源 `workers/model_registry/basins_registry_rows.py:473` 的 `_shud_riv_` 拼接（行号依 #2644 原文，另附符号名）；
  - direct-grid variant `dg_<32hex>`，来源 `workers/model_registry/direct_grid_variant_registration.py::_mint_model_id`；
  - river_segment_id 前缀只来自前者；latest-product / hydro_run 的 `model_id` 可能是 variant。

  另外，两条 reach/output row 词条中的 `<model_id>` 指向该词条。
- [x] 4.5 #2644：runbook「部署组 id」检查（`grep -rn 部署组 docs` 为空，已由 #2643 archive 修正）。

## 5. 验证

- [ ] 5.1 `uv run ruff check .`；`openspec validate basin-catalog-semantics-and-rename-leftovers --strict --no-interactive`。
- [ ] 5.2 node-27 真实 DB pytest：新测试，外加 `tests/test_model_registry*.py` 中与 `list_basins` 相关的用例，以及 selector 测试。
- [ ] 5.3 前端 `runbookContract.test.ts`：只读取 `node-27-bringup-checklist.md` 和 `tier-node27-timeseries-storage.md`。本批如果没改这两个文件，就标为不适用。

## Evidence Floor

- [ ] ruff、openspec validate
- [ ] node-27 真实 DB pytest（`TMPDIR=/home/nwm/tmp`）
- [ ] node-27 处置前后三方集合 receipt、删除 dry-run 与 commit receipt
