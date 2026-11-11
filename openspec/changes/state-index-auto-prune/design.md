# Design: state-index-auto-prune (#3121)

## Governing invariants

1. **timer 必恢复**：本次运行停掉的每个 timer，只要进程还活着能执行收尾（正常返回、异常、SIGTERM/SIGINT/SIGHUP），退出前都要 `start` 恢复，且每个只恢复一次。
   - 不是本次运行停掉的 timer，一律不碰。
   - SIGKILL 是残余风险：stall probe 会报 `timer_stopped`，runbook 给出人工恢复命令。
2. **尽力在 pass 边界写**：enforce 只在以下条件同时满足时执行——调度器 service 和 provider-refresh service 都空闲（`is-active` 为 inactive/failed，且 `systemctl show -p Job --value` 为空，排除已排队但还没启动的 job），并且没有 cycle-once wrapper 在跑。
   - 这条只是尽力而为：wrapper 可能在初查和 stop 之间启动。闸门复查能发现这种情况，此时放弃 enforce 并恢复 timer。最坏情况是一次 pass 与 wrapper 争调度锁（`lock_contended`），不会损坏 index。
   - **正确性的底线**是 index 锁、锁内重新规划和 CAS，见下文"writer 清单"。
3. **窗口不短于 floor**：`retention_days = max(DEFAULT, minimum_state_index_retention_days(lag))`，复用 #3118 的 helper。repair 自身的 `_state_index_retention_window` 仍是最后一道闸。

## writer 清单（runbook 8.9 的冻结范围）

| writer | 处置 | 出处 |
|---|---|---|
| scheduler pass 内的 copyback | 停调度器 timer，并等 service 空闲 → 冻结 | `services/orchestrator/run_tree_copyback.py` 在调度器进程内 |
| provider-refresh | 停它的 timer，并等 service 空闲 → 冻结 | `infra/systemd/nhms-scheduler-file-provider-refresh.*` |
| 人工 copyback replay（`scripts/scheduler_state_index_copyback_replay.py`） | 不冻结。与 copyback 用同一把锁和 CAS（该脚本 :10-15），与 enforce 串行；enforce 在锁内重新规划，不会丢更新 | runbook 8.9 |
| recalibration clone（`scripts/node22_clone_direct_grid_cutover_states.py`） | 不冻结。通过 `FileStateSnapshotIndexRepository.upsert_state_snapshot` 写入（:452），走 `_update_lock`（`.index-last.json.lock`），与 enforce 串行。这是一次性人工工具，runbook 写明不要与自动剪枝同时跑 | state_manager `upsert_state_snapshot` |
| 已提交的 Slurm `state_save_qc` 作业 | pass 结束时这些作业已全部结束（pass 会等它们）。残留作业也走 `_update_lock`，与 enforce 串行 | runbook 8.12"`upsert_state_snapshot`/`set_usable_flag` 与 enforce 共用 `.index-last.json.lock`" |

## 探针在冻结期间的反应

- **stall probe**（每 15 分钟一次，判据是 timer 不是 active 且 service 空闲 → `timer_stopped`，exit 1，经 OnFailure 发邮件）：
  - 冻结窗口只覆盖"pass 结束 → 闸门 → enforce → 恢复"这一段，通常是几十秒；
  - 只有 pass 结束后 provider-refresh 还在跑时窗口才会变长。
  - 接受偶发 1 次 `timer_stopped` 告警。runbook 的 `stuck-detection.md` 写明：先 `pgrep -f node22_state_index_auto_prune`，有在跑就属预期。不改 probe（non-goal）。
- **refresh-timer-health**（`DEFAULT_STOPPED_DWELL_HOURS=8`）：等待上限 5h 加 enforce 的分钟级耗时，远小于 8h，不会误报。等待上限的默认值必须小于 8h，由测试断言。

## 流程（`scripts/node22_state_index_auto_prune.py`，`main() -> int`）

1. **import 与配置**
   - import repair 与 state_manager；失败 → exit 4（与 watch 同口径）。
   - 读 lag 和窗口：直接 `from scripts.node22_state_index_capacity_watch import _retention_window` 并调用它，不改 watch 文件。lag 不合法 → 交给 repair 拒绝，exit 2。
   - receipt 根：`NHMS_STATE_INDEX_AUTO_PRUNE_RECEIPT_ROOT`，必须已存在且 owner-private，不会自动创建，否则 exit 2。
2. **初次 dry-run**
   - 与 watch 相同的 `repair_state_index(operation="prune-retention", enforce=False, retention_days=N, cycle_lag_hours=None, ...)`；`provider_preimage_changed` 最多试 3 次。
   - 任一 lane `capacity_before.warning` 为真才继续，否则写 `action=skip`，exit 0。
   - **预闸门**（不碰 timer）：两个 lane 都 `checksum_valid=true`、`entry_count_before>0`，且每个超阈值的 lane `removed_count>0`。不满足 → `action=gate_failed_precheck`，exit 1。
3. **wrapper 检查**
   - 用 runbook 里锚定的 pgrep 模式，由模块常量给出，测试可注入。
   - pgrep 退出码：0 → 有 wrapper；1 → 没有；≥2、超时或执行失败 → 无法判定，exit 2，fail-closed。
   - 有 wrapper 在跑 → `action=deferred_wrapper_running`，exit 1，不碰 timer。
4. **冻结**
   - 对 `nhms-compute-scheduler.timer` 和 `nhms-scheduler-file-provider-refresh.timer` 分别 `is-active`：
     - `active` → 先登记为"本次停掉"，再 `stop`；stop 失败 → exit 2（收尾会恢复已登记的）；
     - `inactive` / `failed` → 不碰；
     - 其它或无法判定 → exit 2。
   - 轮询两个 service：`is-active` 为 `inactive` / `failed`，**且** `systemctl show -p Job --value <svc>` 为空，才视为空闲；`active` / `activating` / `deactivating` / `reloading`，或者有排队的 job，视为运行中；其它值 → exit 2。
   - 等待上限 `NHMS_STATE_INDEX_AUTO_PRUNE_WAIT_SECONDS`，默认 18000 秒（5h）；轮询间隔 `..._POLL_SECONDS`，默认 30 秒。超时 → `action=wait_timeout`，exit 2。
5. **闸门**
   - 再查一次 wrapper（有 → exit 1）。
   - 再查一次两个 service 都空闲（否则 exit 2）。
   - 最终 dry-run：条件与预闸门相同（两个 lane checksum 都有效、条目数都 >0，每个超阈值的 lane `removed_count>0`）；任一不满足 → `action=gate_failed`，exit 1。
   - receipt 记录两个 lane 的 `removed_state_ids_sha256`。
6. **enforce**（期间信号**延迟处理**：SIGTERM/SIGINT/SIGHUP 的 handler 只记录"收到信号"，不打断进程内的 `repair_state_index`，以免异常落在两次 lane CAS 之间。enforce 返回后如果记录过信号，先写 receipt，`action` 照实际 enforce 结果填，再按 128+n 退出）
   - `repair_state_index(..., enforce=True, retention_days=N)`。archive 与 receipt 根由 repair 自己从 env 读（`NHMS_SCHEDULER_STATE_INDEX_REPAIR_ARCHIVE_ROOT` / `..._RECEIPT_ROOT`，unit 里给出）。
   - `RepairIncompleteError` → `action=enforce_incomplete`，exit 3。
   - `RepairCliError` → exit 2。
   - 成功：与闸门 digest 比对，不一致只记录、不当作失败（enforce 在锁内会重新规划）。
   - `capacity_after.warning` 仍为真 → `action=enforced_unprunable`，exit 1；否则 `action=enforced`，exit 0。
7. **收尾**（try/finally 加信号处理）
   - 对每个登记为"本次停掉"的 timer 执行 `start`。
   - 任一 start 失败 → 记 `timer_restore_failed`，exit 5，覆盖前面的退出码。日志打印人工恢复命令，不打印 secret 或路径以外的内容。
   - 信号处理：enforce 之外的阶段收到 SIGTERM / SIGINT / SIGHUP，抛出 `SystemExit(128+n)` 进入 finally；enforce 期间按第 6 步延迟处理。
   - **一进入 finally 就把三个信号都设为 `SIG_IGN`**（照 wrapper `on_exit` 的先例），保证恢复循环不会被第二个信号打断，每个 timer 恰好 start 一次。
   - 每次 systemctl / pgrep 子进程调用都带 `timeout=60s`，超时按"无法判定"处理；收尾阶段 start 超时记为 restore 失败。
   - receipt 先打到 stdout（进 journal），再落盘（与 watch 相同）。

## 可注入

- `NHMS_NODE22_SYSTEMCTL`（默认 `systemctl --user`），用 `shlex.split` 解析，测试时传入假脚本。
- pgrep 命令：`NHMS_STATE_INDEX_AUTO_PRUNE_PGREP`，默认是锚定模式的 `pgrep -u <uid> -f '<pattern>'`。
- 时钟和 sleep 通过模块级函数注入，测试中 monkeypatch。

## Receipt（`nhms.scheduler.state_index_auto_prune_receipt.v1`）

- 顶层字段：`started_at`、`finished_at`、`status`、`action`、`exit_code`、`retention_days`、`retention_days_source`、`cycle_lag_hours`、`timers_stopped[]`、`timers_restored[]`、`waited_seconds`、`gate`、`enforce`、`error`。
  - `gate`：每个 lane 的 checksum、条目数、removed 数和 digest。
  - `enforce`：每个 lane 的 status、removed 数、`capacity_before` / `capacity_after`、digest。
- 不含 `removed_state_ids` 和 `groups`。
- 大小上限 1 MiB。根目录 0700、文件 0600，保留最新 30 份加 `latest.json`（照抄 watch 的写法）。

## systemd

`infra/systemd/nhms-scheduler-state-index-prune.service`：
- `Type=oneshot`、`WorkingDirectory=/scratch/frd_muziyao/NWM`；
- `EnvironmentFile=…/compute.scheduler-dbfree.env`；
- `Environment=` 给出三个根：auto-prune receipt、repair archive、repair receipt；
- `ExecStart=/scratch/frd_muziyao/NWM/.venv/bin/python -m scripts.node22_state_index_auto_prune`；
- `TimeoutStartSec=21600`（超过 5h 等待加 enforce）；`KillSignal=SIGTERM`，保证超时时能进入收尾恢复 timer；
- `TimeoutStopSec=300`。预算从 systemd 发出 SIGTERM 起算，要覆盖"剩余 enforce + 收尾 + 写 receipt"：
  - enforce 实测 14s（node-22 2026-11-11 15:52:08→15:52:22Z，两个 lane 各剪 1282 条）；按 4 倍余量计 60s。
  - 两个 timer 的 start 各有 60s 超时，共 120s；写 receipt 计 10s。
  - 合计 190s，小于 300s。
  - runbook 里说的"约 5 分钟调度暂停"指的是人工流程的整体耗时，不是 enforce 本身。
  - 残余风险：如果 enforce 异常地慢、超过约 170s，仍会在 enforce 中途收到 SIGKILL。这种情况按 exit 3 走人工处置：重新 dry-run，比对两个 lane 的条目数，必要时手动 enforce。runbook 8.12 给出命令。
- 已在 node-22 实测 `systemctl --user show -p Job --value`：service 处于 activating 时输出 job id（例如 `304554`），inactive 时输出为空。
- `OnFailure=nhms-node22-unit-failure-alert@%n.service`（与 watch 相同）。

`.timer`：`OnCalendar=*-*-* 06:30:00 UTC`、`RandomizedDelaySec=15m`、`Persistent=true`。

## Sibling surfaces（新 unit 必须登记进的全量清单）

- `infra/systemd/node22/MANIFEST.json`：登记新的 service 和 timer 及其 sha256（与 capacity watch 同样处理）。
- `tests/test_node22_entrypoint_invariant.py`：新 unit 的 ExecStart、schedule 断言，以及 `:517` 列表。
- `tests/test_node22_unit_tracking.py:44` 的 `TOP_LEVEL_ONESHOTS`。
- `tests/test_node22_unit_failure_alert.py:483` 的 `WIRED_UNITS`，以及 `:526` 的"恰好六个"用例，改为七个。
- `tests/test_node22_entrypoint_invariant_python_scan.py:274` 的 governed 列表。
- `scripts/select_ci_tests_rules.py:201-203`：按 watch 的写法，加入新的 script/unit/test 规则；同步 `tests/test_select_ci_tests.py`。
- spec：`openspec/specs/node22-unit-failure-alert/spec.md` 把"恰好六个 oneshot"改为七个（本 change 带 MODIFIED delta）。
- runbook：8.12（Stage 2 决策、安装/启用、退出码表、`timer_stopped` 期间的预期行为）；`stuck-detection.md` 的 `timer_stopped` 处置（auto-prune 持有 timer 时属预期，先 `pgrep -f node22_state_index_auto_prune` 再 start）。
- 现有的 stall probe、cycle-once wrapper、watch 都不改。

## Must preserve

- repair 的 enforce 语义与锁序；`_state_index_retention_window`；watch 的行为与退出码；cycle-once wrapper 的行为。
- 不是本次运行停掉的 timer 绝不 start。
- secret 不进日志。

## Required evidence（假 systemctl + 假 repair + 假 pgrep；新文件 `tests/test_node22_state_index_auto_prune.py`）

1. 没到阈值 → 没有任何 systemctl stop/start，enforce 未被调用，exit 0，`action=skip`。
2. 到阈值：
   - 调用顺序严格为 stop 两个 timer → 轮询直到 inactive → 最终 dry-run → enforce → start 两个 timer；
   - exit 0，`action=enforced`；
   - `retention_days` 在 lag 294 时为 29，lag 16 时为 21。
3. wrapper 在跑（初查）→ 没有 stop，exit 1；wrapper 在闸门复查时出现 → 不 enforce，timer 恢复，exit 1。
4. 等待超时 → 不 enforce，timer 恢复，exit 2，`action=wait_timeout`。
5. 冻结后闸门失败（初次 dry-run 通过、最终 dry-run 失败；分别测 checksum 无效、条目为 0、超阈值 lane 的 removed 为 0）→ 不 enforce，timer 恢复，exit 1，`action=gate_failed`。
6. enforce 抛 `RepairIncompleteError` → timer 恢复，exit 3；抛 `RepairCliError` → timer 恢复，exit 2。
7. 剪完仍 ≥ 0.70 → exit 1，`action=enforced_unprunable`。
8. 进入时 provider-refresh timer 就是 inactive → 只 stop/start 调度器的 timer。
9. timer stop 失败 → 恢复已经停掉的那个，exit 2。
10. timer start 失败 → exit 5，日志里有人工恢复命令。
11. SIGTERM：在等待中途发送真实信号（子进程运行模块，配合假 systemctl 和假 sleep）→ timer 恢复，退出码为 143。
12. service 状态无法判定 → exit 2，timer 恢复。
13. receipt 落盘的权限、保留 30 份；receipt 里没有 `removed_state_ids`。
14. 新 unit 的 entrypoint invariant：ExecStart 用精确解释器加 `-m`；schedule 正确；`TimeoutStartSec` 大于等待上限；`TimeoutStopSec` ≥ 60（enforce 余量）+ 2 × 子进程超时 + 10，即模块常量 `ENFORCE_STOP_BUDGET_SECONDS`、`SUBPROCESS_TIMEOUT_SECONDS` 推出的值；等待上限默认值小于 refresh-timer-health 的 8h dwell；子进程超时常量为 60。
15. enforce 期间收到 SIGTERM（假 repair 在调用中自行 `os.kill(os.getpid(), SIGTERM)`）→ enforce 照常完成；receipt 的 `action=enforced`，并带 `signal=SIGTERM`；timer 恢复；退出码 143。
16. 收尾恢复期间再收到一次 SIGTERM（假 systemctl 在第一次 start 时给父进程发信号）→ 两个 timer 仍各 start 一次。
17. pgrep 退出码为 2 或超时 → exit 2，不碰 timer。
18. service 有排队的 job（`show -p Job` 非空）时，即使 `is-active=inactive` 也继续等待。
19. 预闸门：超阈值的 lane `removed_count=0` → 不碰 timer，exit 1，`action=gate_failed_precheck`；未超阈值的 lane `removed_count=0` 时可以继续。

每个新测试都应先红：新模块不存在时测试应失败。
