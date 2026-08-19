## 1. Implementation

- [x] 1.1 (#1405) 新建 `services/orchestrator/run_identity.py`：
      `FORECAST_RUN_ID_RE`/`CYCLE_COHORT_RUN_ID_RE`（正则逐字迁自
      file_orchestration_journal.py:174/:176；`_CYCLE_RUN_ID_RE`:175 严格
      变体留在 journal 不迁）+ 新增
      `ANALYSIS_RUN_ID_RE = ^analysis_([^_]+)_(\d{10})_(\d{10})_(.+)$`
      （cycle=第一个 10 位段=start，对齐 chain_analysis.py:44/:57）+
      `parse_run_cycle(run_id) -> datetime | None`（三形状之一匹配 →
      canonical cycle 段 strptime `%Y%m%d%H` UTC aware；不匹配或 strptime
      失败 → None）
- [x] 1.2 (#1405) `file_orchestration_journal.py` 两正则改从 run_identity
      导入并保留旧私有名别名，模块内 6 行/7 处引用零改动
      （:5938/:5941/:8451/:8460/:9486/:9772）
- [x] 1.3 (#1405) `retention.py` `_extract_run_cycle` 改 delegate
      `parse_run_cycle`；`_parse_cycle_name` 与 cycles/ 层扫描不动
- [x] 1.4 (#1503) `cli.py`：`_cleanup_frontier` 回传 evidence_dir；blocker
      payload 加 `"evidence_dir"`（解析成功=绝对路径；unresolved=显式
      null）；ok 路径顶层 payload 加同名键（与 frontier_source 并列）
- [x] 1.5 (#1503) 归档副本
      `openspec/changes/archive/2026-08-17-retention-frontier-out-of-pass/design.md`
      **只追加 supersession 指针**（形状经 #1503/retention-lane-hygiene
      扩展，现行权威=openspec/specs/production-scheduler-orchestration），
      :88-93 原文不动（DOC_STATUS 归档即证据）
- [x] 1.6 (#1395) 删 `packages/common/storage.py` helper 族+常量+
      `ArchiveConfigurationError`；`DEFAULT_RETENTION_WINDOW_DAYS`:46 与
      :40-45 注释保留（注释措辞按 extractor 已删微调）；删
      `tests/test_storage.py` retention 段（110 条+xfail 哨兵+bash oracle），
      保留 validate_object_path 17 条 + override precedence 2 条

## 2. Tests

- [x] 2.1 (#1405) run_identity 直测（新测试或并入 tests/test_retention.py）：
      forecast/cohort（含尾段）/analysis 三形状取对 cycle（analysis →
      start 段）；`fcst_2020010100_2026081400_model_a` → 2026081400（A 类
      修正）；`manual_salvage_2020010100_keepme` → None；
      `fcst_gfs_<cycle>_model_2026010100` → cycle（尾部误报不干扰）；
      `fcst_gfs_2026139999_model_2026010100` → None（canonical 位非法日期，
      不回落尾部 token）；大写 `FCST_...` → None；
      `fcst_x_2026139999_y` → None（loose 也 None，非翻转，直测锁定）
- [x] 2.2 (#1405) retention 面行为钉（tests/test_retention.py，tmp_path 造
      `runs/<name>` 真目录喂 `_collect_run_targets` 或其公共入口）：
      B 类目录 → `skipped: unparseable_run_cycle`（**改动前红：进 planned
      删除**）；A 类目录 → 按正确 cycle 裁决（改动前红：按 2020 判老删除）；
      过期 forecast / 过期 cohort（带尾段）/ 过期 analysis → 仍进删除目标
      （回收不变锁；analysis 腿为 P1-1 翻转防护，改动前后均可回收）；
      frontier 豁免/窗口内跳过两层裁决不变锁
- [x] 2.3 (#1503) tests/test_cli_cleanup_frontier.py：
      `evidence_dir_missing` 与 `no_readable_receipt` 两 reason 下 blocker
      携非空 `evidence_dir` == 实际探测绝对路径（**改动前红：键不存在**）；
      `evidence_dir_unresolved` → 键存在值 null 且 CLI 不抛；ok 路径顶层
      payload 携 `evidence_dir`；既有 fail-closed 断言零改动全绿
- [ ] 2.3b (#1503, round-1 T1) 派生臂判别腿：delenv
      `NHMS_SCHEDULER_EVIDENCE_ROOT` + 相对 `WORKSPACE_ROOT` +
      `monkeypatch.chdir(tmp_path)`，断言 blocker 的 `evidence_dir`
      `is_absolute()` 且 == 派生路径 `<cwd>/<workspace>/scheduler/evidence`
      ——钉住「absolute path actually probed」的 spec 口径（env 回显 mutation
      下该腿红），覆盖 #1503 动机场景（错 cwd 相对默认可区分）
- [x] 2.4 (#1395) 删除后残留 grep（见 3.2）+ 保留面三锁复跑绿：
      `DEFAULT_RETENTION_WINDOW_DAYS` identity+值双钉、validate_object_path
      17 条、override precedence 2 条

## 3. Verification

- [x] 3.1 uv run pytest -q tests/test_retention.py
      tests/test_cli_cleanup_frontier.py tests/test_retention_frontier.py
      tests/test_storage.py tests/test_node27_timeseries_retention.py
      tests/test_file_orchestration_journal.py
- [x] 3.2 (#1395) `grep -rn "read_retention_window_days\|_scan_env_assignment\|_EnvAssignmentScan\|_ENV_ASSIGNMENT_PATTERN\|RETENTION_ENV_PATH_VARIABLE\|RETENTION_WINDOW_VARIABLE\|RETENTION_VARIABLE_PREFIX\|ArchiveConfigurationError" --include="*.py" .`
      在 `openspec/changes/archive/**` 之外零命中；
      `packages/common/storage.py` 中 `DEFAULT_RETENTION_WINDOW_DAYS = 14`
      原样存在、`scripts/node27_timeseries_retention.py:74` import 未改
- [x] 3.3 uv run ruff check .
- [x] 3.4 openspec validate retention-lane-hygiene --strict --no-interactive
- [x] 3.5 PR body 回链 #1233 两条评论（PR #1585）（issuecomment-5154539095 /
      issuecomment-5300567380）
- [ ] 3.6 merge 后 node-27 receipt（3.1 六套件；全量红按 #1513 已知例外
      口径核对）分别记 #1395/#1405/#1503
