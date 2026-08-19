## 1. Implementation

- [ ] 1.1 (#1405) 新建 `services/orchestrator/run_identity.py`：
      `FORECAST_RUN_ID_RE`/`CYCLE_COHORT_RUN_ID_RE`（正则逐字迁自
      file_orchestration_journal.py:169/:176）+
      `parse_run_cycle(run_id) -> datetime | None`（形状匹配 → cycle 段
      strptime `%Y%m%d%H` UTC aware；不匹配或 strptime 失败 → None）
- [ ] 1.2 (#1405) `file_orchestration_journal.py` 两正则改从 run_identity
      导入并保留旧私有名别名，模块内用点零改动
- [ ] 1.3 (#1405) `retention.py` `_extract_run_cycle` 改 delegate
      `parse_run_cycle`；`_parse_cycle_name` 与 cycles/ 层扫描不动
- [ ] 1.4 (#1503) `cli.py`：`_cleanup_frontier` 回传 evidence_dir；blocker
      payload 加 `"evidence_dir"`（解析成功=绝对路径；unresolved=显式
      null）；ok 路径顶层 payload 加同名键（与 frontier_source 并列）
- [ ] 1.5 (#1503) 归档副本
      `openspec/changes/archive/2026-08-17-retention-frontier-out-of-pass/design.md`
      blocker 形状段同步（含 ok 路径与 null 裁决的落地记录）
- [ ] 1.6 (#1395) 删 `packages/common/storage.py` helper 族+常量+
      `ArchiveConfigurationError`；`DEFAULT_RETENTION_WINDOW_DAYS`:46 与
      :40-45 注释保留（注释措辞按 extractor 已删微调）；删
      `tests/test_storage.py` retention 段（110 条+xfail 哨兵+bash oracle），
      保留 validate_object_path 17 条 + override precedence 2 条

## 2. Tests

- [ ] 2.1 (#1405) run_identity 直测（新测试或并入 tests/test_retention.py）：
      forecast/cohort 合法形状取对 cycle；`fcst_2020010100_2026081400_model_a`
      → 2026081400（A 类修正）；`manual_salvage_2020010100_keepme` → None；
      `fcst_gfs_<cycle>_model_2026010100` → cycle（尾部误报不干扰）；
      10 位非法日期 token（如 2026139999）→ None
- [ ] 2.2 (#1405) retention 面行为钉（tests/test_retention.py，tmp_path 造
      `runs/<name>` 真目录喂 `_collect_run_targets` 或其公共入口）：
      B 类目录 → `skipped: unparseable_run_cycle`（**改动前红：进 planned
      删除**）；A 类目录 → 按正确 cycle 裁决（改动前红：按 2020 判老删除）；
      过期 forecast 与过期 cohort → 仍进删除目标（回收不变锁）；
      frontier 豁免/窗口内跳过两层裁决不变锁
- [ ] 2.3 (#1503) tests/test_cli_cleanup_frontier.py：
      `evidence_dir_missing` 与 `no_readable_receipt` 两 reason 下 blocker
      携非空 `evidence_dir` == 实际探测绝对路径（**改动前红：键不存在**）；
      `evidence_dir_unresolved` → 键存在值 null 且 CLI 不抛；ok 路径顶层
      payload 携 `evidence_dir`；既有 fail-closed 断言零改动全绿
- [ ] 2.4 (#1395) 删除后残留 grep（见 3.2）+ 保留面三锁复跑绿：
      `DEFAULT_RETENTION_WINDOW_DAYS` identity+值双钉、validate_object_path
      17 条、override precedence 2 条

## 3. Verification

- [ ] 3.1 uv run pytest -q tests/test_retention.py
      tests/test_cli_cleanup_frontier.py tests/test_retention_frontier.py
      tests/test_storage.py tests/test_node27_timeseries_retention.py
      tests/test_file_orchestration_journal.py
- [ ] 3.2 (#1395) `grep -rn "read_retention_window_days\|_scan_env_assignment\|_EnvAssignmentScan\|_ENV_ASSIGNMENT_PATTERN\|RETENTION_ENV_PATH_VARIABLE\|RETENTION_WINDOW_VARIABLE\|RETENTION_VARIABLE_PREFIX\|ArchiveConfigurationError" --include="*.py" .`
      在 `openspec/changes/archive/**` 之外零命中；
      `packages/common/storage.py` 中 `DEFAULT_RETENTION_WINDOW_DAYS = 14`
      原样存在、`scripts/node27_timeseries_retention.py:74` import 未改
- [ ] 3.3 uv run ruff check .
- [ ] 3.4 openspec validate retention-lane-hygiene --strict --no-interactive
- [ ] 3.5 PR body 回链 #1233 两条评论（issuecomment-5154539095 /
      issuecomment-5300567380）
- [ ] 3.6 merge 后 node-27 receipt（3.1 六套件；全量红按 #1513 已知例外
      口径核对）分别记 #1395/#1405/#1503
