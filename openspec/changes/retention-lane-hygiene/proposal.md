# Proposal: retention-lane-hygiene (#1395 + #1405 + #1503)

## Why

三个 retention/cleanup 车道卫生单合并交付（按用户批量指令分组；三者改动面互不
相交，共享同一验证邻域）：

- **#1395（死岛退役，零行为变更）**：`packages/common/storage.py` 的 retention
  env extractor 族（`read_retention_window_days:106-231` + `_EnvAssignmentScan`
  `_scan_env_assignment` `_strip_env_trailing_comment` `_unquote_env_value` +
  常量 `:48-52,:59` + `ArchiveConfigurationError:31-37`）自 #1370 删除两个
  归档车道消费者后零生产调用，仅被 `tests/test_storage.py` 的 110 条测试拽着
  （含 per-row 真 bash 差分对拍与两条永不翻转的 strict-xfail 哨兵——等待的
  #1233 已 obsolete 关闭）。docstring 仍以现在时宣称守护活 retention 窗口，
  构成误接线诱饵（helper 有两类已知 fail-open 残差）。
- **#1405（删除判据收敛，fail-safe 方向行为变更）**：
  `services/orchestrator/retention.py:177-183` `_extract_run_cycle` 按 `_`
  切 token 取第一个可解析 `%Y%m%d%H` 的片段、不校验 run_id 整体形状，唯一
  调用点 `:289`（`_collect_run_targets`）直接喂删除 cutoff 与 frontier 豁免
  闸。与 journal canonical 解析（`_FORECAST_RUN_ID_RE:169`、
  `_CYCLE_COHORT_RUN_ID_RE:176`）宽严分叉：A 类取错 token（当前构造路径
  不可达，形状防御）；B 类过度接受——`runs/` 下任何含 10 位数字 token 的
  非 run 目录（人工 salvage、调试留存）被当过期工作区删除，严格侧本会拒收。
- **#1503（blocker 可观测性，纯加字段）**：`services/orchestrator/cli.py:152-156`
  frontier_blocker 只有 `{reason, forced_dry_run, receipt_path}`；三个最常见
  reason（`evidence_dir_missing`/`no_readable_receipt`/`evidence_dir_unresolved`）
  下 `receipt_path` 恒 null。`WORKSPACE_ROOT` 缺省相对路径 `.nhms-workspace`
  在错 cwd 下静默指向空目录，与「evidence 真没了」不可区分——保留面长期
  哑火无人察觉。ok 路径同样不报目录。PR #1501 round-1 DEFER 项。

## What Changes

**#1405（先做——它决定新模块形状）**：
- 新建 `services/orchestrator/run_identity.py`（小模块，零 services 依赖）：
  迁入 `FORECAST_RUN_ID_RE`、`CYCLE_COHORT_RUN_ID_RE` 两个 canonical 正则 +
  新函数 `parse_run_cycle(run_id) -> datetime | None`（匹配 forecast 或
  cohort 形状 → 取 cycle 段 strptime `%Y%m%d%H`（UTC aware，与
  retention `_parse_cycle_name` 口径一致）；形状不匹配或 strptime 失败 →
  None）。
- `file_orchestration_journal.py`：`_FORECAST_RUN_ID_RE`/`_CYCLE_COHORT_RUN_ID_RE`
  改为从 run_identity 导入并保留旧私有名别名（模块内 4 个用点零改动）。
- `retention.py`：`_extract_run_cycle` 改为 delegate `parse_run_cycle`
  （或直接替换调用点）；不匹配 canonical 形状 → `unparseable_run_cycle`
  跳过（保留，fail-safe）。行为变更钉住：`manual_salvage_<10位>_keepme`
  从「删除」变「保留」；`fcst_<10位>_<cycle>_model_a` 从「取错 token」变
  「取 cycle 段」；`cycle_gfs_<cycle>` cohort 必须仍可回收（现行为保持）。
- `_parse_cycle_name`（cycle 目录名解析，`:253` 消费）不动——它解析的是
  `cycles/` 层目录名，本就是纯 `%Y%m%d%H`，不在本单口径内。

**#1503**：
- `cli.py` `_cleanup_frontier` 回传已解析 `evidence_dir`；blocker payload 增
  `"evidence_dir"` 键：解析成功 → 绝对路径字符串；`evidence_dir_unresolved`
  → 显式 null（键存在，形状稳定）。ok 路径顶层 payload 同样加
  `evidence_dir`（与 `frontier_source` 并列）——成功/失败两路口径一致。
- 同步修订归档副本
  `openspec/changes/archive/2026-08-17-retention-frontier-out-of-pass/design.md`
  的 blocker 形状段（:88-93）+ 本 change 对
  `production-scheduler-orchestration` 既有条款的 MODIFIED delta。
- fail-closed 判定逻辑零变更（纯加字段）。

**#1395**：
- 删除 helper 族 + 常量 + `ArchiveConfigurationError`（失去最后 raiser，
  裁决=删除）+ `tests/test_storage.py` 整个 retention 段（110 条，含
  strict-xfail 哨兵与 bash 差分 oracle）。
- 保留 `DEFAULT_RETENTION_WINDOW_DAYS = 14`（`:46`）及 `:40-45` drift-lock
  注释（措辞按「extractor 已删」现实微调，常量与值不动）；
  `scripts/node27_timeseries_retention.py:74` import 不动；
  `tests/test_node27_timeseries_retention.py:457-458` identity+值双钉不动。
- PR body 回链 #1233 两条评论（终版白名单文法+攻击语料 /
  obsolete 关闭证据），知识以链接形态保存。

## Non-Goals

- #1405 不改 cutoff/frontier 判定语义本身，只换 cycle 提取口径；不动
  node-27 raw-retention 面。
- #1503 不改 `WORKSPACE_ROOT` 缺省与 evidence_dir 回落语义（scheduler_config
  既有契约）；不动 fail-closed 决策。
- #1395 不碰 `scripts/node27_timeseries_retention.py` 自己的 env 解析；不碰
  `infra/env/*.example`；#1240 同族死岛独立裁决不并入。
- journal 的 run_id 消费点（:5941/:8460 等）行为不变（仅 import 来源改变）。

## Risk triage

- Fixture level: compact+（三单合批，但写集互不相交；#1405 是唯一真行为
  变更）。Repair intensity: low。
- Risk packs: state-semantics selected（#1405 删除判据真值表：canonical 可
  解析 → 原三层裁决不变；不可解析 → 保留。B 类翻转方向 = fail-safe，
  A 类修正方向 = 取对 cycle；cohort 回收不变）；test-evidence selected
  （对抗性 run_id 四元组红证：A 类、B 类、尾部误报、cohort 仍可回收；
  #1503 两个 reason 的 evidence_dir 断言；#1395 删除后残留 grep）；其余
  not selected。

## Must preserve

- #1405：合法 forecast/cohort run_id 的回收行为逐字不变（cutoff、frontier
  豁免、within_retention_window 三层裁决顺序不动）；journal 内 4 个正则
  用点行为不变。
- #1503：blocker 既有三键不动；fail-closed/dry-run 强制不动；
  `tests/test_cli_cleanup_frontier.py` 既有断言零改动全绿。
- #1395：`DEFAULT_RETENTION_WINDOW_DAYS` identity+值双钉
  （`tests/test_node27_timeseries_retention.py:457-458`）仍绿；
  `tests/test_storage.py` 保留的 `validate_object_path` 17 条 + override
  precedence 2 条仍绿。

## Seams under test

- run_identity 直测 + retention `_collect_run_targets` 目录构造（tmp_path
  下造 `runs/<name>` 目录喂真实扫描）；cli cleanup payload JSON 断言
  （tests/test_cli_cleanup_frontier.py 既有 fixture 复用）；storage 删除面
  grep 残留清单。

## Evidence mapping

- #1405 验收（判据收敛+四元组红证+cohort 不变）→ tasks 2.1/2.2。
- #1503 验收（两 reason 的 evidence_dir + ok 路径 + 形状合法）→ tasks 2.3。
- #1395 验收（残留 grep 清单 + 保留面三锁）→ tasks 2.4 + 3.2。
- Verification：`uv run pytest -q tests/test_retention.py
  tests/test_cli_cleanup_frontier.py tests/test_retention_frontier.py
  tests/test_storage.py tests/test_node27_timeseries_retention.py
  tests/test_file_orchestration_journal.py` + ruff + openspec validate。
