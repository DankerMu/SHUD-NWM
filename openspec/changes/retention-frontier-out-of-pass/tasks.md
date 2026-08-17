# Tasks: retention-frontier-out-of-pass

## 1. 实现

- [x] 1.1 新模块 `services/orchestrator/retention_frontier.py`（design D1）：
      `read_latest_pass_frontier(evidence_dir, *, now, max_age)` →
      `FrontierReadResult`；扫描排除 `*.pre_execution.json`、最新按
      receipt 内 `started_at` 选取、并列取文件名字典序最大；单文件读取
      上限沿用 `MAX_EVIDENCE_BYTES` 口径；unavailable reason 按 D1 唯一
      枚举——helper 产出其中 7 个（含 `pass_retention_not_run`/
      `frontier_bound_invalid`/`frontier_read_error`），
      `evidence_dir_unresolved` 由 cli.py 包裹层产出、同一命名空间同走
      `frontier_blocker`（D1 脚注，N3）；选中 receipt 后先判
      `retention.status ∈ {disabled,error}` 再判键缺失（D1 判定优先级，
      N2）；env `NHMS_RETENTION_FRONTIER_MAX_AGE_HOURS` 默认 24。
- [x] 1.2 `cli.py` `_run_cleanup` 接入（design D2/D3）：ok ⇒ 透传
      bound/source + payload 顶层 `frontier_source`（null 镜像标签载体，
      P1-1）；unavailable ⇒ 强制 dry-run + payload 顶层
      `frontier_blocker`；evidence_dir 派生用
      `ProductionSchedulerConfig().evidence_dir` 且派生在 try 内（失败 ⇒
      `evidence_dir_unresolved`）；helper 异常包裹不逃逸；不加任何绕过
      flag；`pass_retention_not_run` 恢复路径写注释（D2）。cleanup 的
      「receipt」= stdout JSON payload（cli.py:467/:779），不落盘。
- [x] 1.3 `scripts/node27_raw_retention.py`（design D5）：`enabled`/`dry_run`
      **env 闸**——`NODE27_RAW_RETENTION_ENABLED`（默认 true）/
      `NODE27_RAW_RETENTION_PLAN_ONLY`（默认 false；**刻意不复用旧名
      `NODE27_RAW_RETENTION_DRY_RUN`**——旧默认反转，遗留实配行会静默打回
      零删除，N5）（默认逐位保持现行为；**CLI flag 保持移除**，
      `test_node27_raw_retention_dry_run_cli_is_removed` 继续绿——本项是
      对 9c1625ee execute-only 裁定 env 面的显式取代，见 D5）+ summary
      `anchor` 披露块（decision + residual_risk）+ SCHEMA_VERSION v3。
- [x] 1.4 env 模板：`infra/env/compute.example` 注释新增
      `NHMS_RETENTION_FRONTIER_MAX_AGE_HOURS`；
      `infra/env/node27-raw-retention.example` 补两个闸 env 注释（按 1.3
      两个 env 名；写明取代旧 evidence floor 而非打破，及不复用旧名的
      原因）。

## 2. 测试（先红后绿；红证锚定 2.2 与 2.3 主形）

- [x] 2.1 `tests/test_retention_frontier.py`：D3 判定表逐格——ok(bound)/
      ok(null)/stale/missing dir/no readable receipt/no frontier block/
      `pass_retention_not_run`（status=disabled 与 error 两形）/bound 畸形/
      读取异常包裹/派生失败；`started_at` 选取正确性（两份 receipt，
      mtime 与 started_at 逆序，选 started_at 大者）；**pre_execution 格**
      （P1-2）：同 `started_at` 的 `X.json` + `X.pre_execution.json` ⇒
      选中 `X.json`；只有 `X.pre_execution.json` ⇒
      `unavailable/no_readable_receipt`（排除规则下与空目录同形，N1）；
      disabled 形（有 `retention` 键无 `frontier` 块）必须落
      `pass_retention_not_run` 而非 `frontier_block_missing`（优先级锚，
      N2）。
- [x] 2.2 I1 主锚（红-绿）：追赶场景——evidence dir 放新鲜 receipt（bound
      = 某 cycle T），object store 有老于墙钟 cutoff 但 >= T 的 cycle 目录
      → `cleanup --execute` 后目录仍在、receipt skipped 含
      `pipeline_frontier_exempt`（接线前红：目录被真删）。
- [x] 2.3 I2 主锚（红-绿）：无 receipt（或过旧）+ `--execute` → 强制
      dry-run、目标目录仍在、receipt 顶层 `frontier_blocker.reason` 正确
      （接线前红：无保护真删）。
- [x] 2.4 I3：新鲜 receipt bound=null → 纯墙钟删除照旧（镜像 pass，不比
      pass 严）；**镜像标签断在 payload 顶层 `frontier_source ==
      "receipt:none"`，frontier 块内 source 按 retention.py:114 恒为
      null**（P1-1，不改 pass 侧 `frontier()`）。
- [x] 2.5 I4：click 入口与 argparse 入口各至少 1 条端到端（AC3，当前
      `grep _run_cleanup tests` 为空）；两入口对同一夹具行为一致。
- [x] 2.6 I5 回归：`tests/test_retention.py` 全绿；`run_retention` 既有
      调用面（scheduler pass）零改动（diff 断言级：pass 内文件不动）。
- [x] 2.7 I6/I7：`tests/test_node27_raw_retention.py`——默认 env 下行为
      逐位回归（既有用例不改断言）；`enabled=false` ⇒ status=disabled 零
      删除；`dry_run=true` ⇒ targets 照常收集、零 rmtree；summary 含
      anchor 块（mode/decision/residual_risk 三键钉值）。

## 3. 验证（Evidence Floor）

- [x] 3.1 `uv run pytest -q tests/test_retention.py
      tests/test_node27_raw_retention.py tests/test_retention_frontier.py`
      通过（含 cleanup 入口测试所在文件）。
- [x] 3.2 `uv run ruff check .` 通过。
- [x] 3.3 `openspec validate retention-frontier-out-of-pass --strict
      --no-interactive` 通过。
- [x] 3.4 面 B 裁定双载体一致性人工对读（design D4 / spec delta 场景）；
      issue 评论作为 PR body 的转述，不作为 evidence 项（fixture review
      P2-4）。
