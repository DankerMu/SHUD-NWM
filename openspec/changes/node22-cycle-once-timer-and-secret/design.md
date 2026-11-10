# Design: node22-cycle-once-timer-and-secret

Change surface: `scripts/ops/node22-run-cycle-once.sh`（唯一代码改动）；`tests/test_production_scheduler.py` 中的 wrapper 测试；runbook `docs/runbooks/production-ops/known-issues-pipeline.md`（exact-cycle 小节）、`docs/runbooks/production-ops/file-provider-refresh.md` 约 :992、`docs/runbooks/node22-control-plane-manual-recovery.md` 约 :420、`docs/runbooks/production-ops/stuck-detection.md` 约 :217-221（`timer_stopped` 处置）。

Must preserve:
- CLI 参数集合与 usage：`--cycle-time`、`--source`、`--basin-id`、`--repair-missing-forcing`、`--plan|--submit`；未知参数 exit 2；缺 `--cycle-time` exit 2；env file 缺失 exit 2。
- `NHMS_NODE22_REPO` / `NHMS_NODE22_SCHEDULER_ENV` 的覆盖语义。
- `plan-production` 的参数顺序：`"${SOURCE_ARGS[@]}" "${BASIN_ARGS[@]}" --cycle-time <t> --disable-backfill <mode>`，以及 python 入口 `$REPO/.venv/bin/python -m services.orchestrator.cli`。
- `NHMS_SCHEDULER_REPAIR_MISSING_FORCING` 由 flag 决定，覆盖 env file 中的陈旧值；未带 flag 时 `..._CYCLE_TIME` 被 unset。
- `NHMS_SERVICE_ROLE` / `SLURM_GATEWAY_URL` / `NHMS_PRODUCTION_SLURM_ENABLED` 的默认值语义。
- 现有两个 `test_node22_exact_cycle_wrapper_*` 测试的断言保持不变，只允许补环境注入（假 systemctl、secret 路径）。

Must add/change:
- 新增环境变量，均可覆盖：
  - `NHMS_NODE22_GATEWAY_SECRET_ENV`，默认 `/scratch/frd_muziyao/nhms-prod/secrets/slurm-gateway.env`，与 `infra/systemd/node22/nhms-compute-scheduler.service.d/10-slurm-gateway-token.conf` 中第二个 `EnvironmentFile` 相同；
  - `NHMS_NODE22_SYSTEMCTL`，默认 `systemctl --user`；
  - `NHMS_NODE22_SCHEDULER_TIMER`，默认 `nhms-compute-scheduler.timer`；
  - `NHMS_NODE22_SCHEDULER_SERVICE`，默认 `nhms-compute-scheduler.service`；
  - `NHMS_NODE22_TIMER_WAIT_SECONDS`，默认 14400；轮询间隔 `NHMS_NODE22_TIMER_POLL_SECONDS`，默认 15。
- 加载顺序：scheduler env → secret env（`set -a`）→ 现有 export → `NHMS_PYTHON_VENV_BIN="${NHMS_PYTHON_VENV_BIN:-$REPO/.venv/bin}"`。
- secret 缺失或不可读：
  - `--submit`：exit 2，stderr 只写路径与原因，不碰 timer，也不运行 python；
  - `--plan`：stderr 告警后继续。
- **systemctl `is-active` 的分类规则**（timer 判定与 service 轮询共用）：先看输出，不看返回码。真实 systemctl 只有 `active` 和 `reloading` 返回 0，其余状态（`inactive`、`failed`、`activating`、`deactivating`、`unknown`）都返回 3；oneshot 的 pass 运行期间输出 `activating`、返回码 3，这是正常情况。实现上，用 `state=$(systemctl is-active <unit> 2>/dev/null) || true` 拿到输出，再用 `case` 分类。只有输出为空或不认识，才算"无法判定"。
- timer 协调发生在参数与 env 校验之后、python 之前：
  1. 初始化 `PY_PID=""`、`TIMER_STOPPED=0`、`RESTORED=0`。**先装 trap**：`trap on_exit EXIT` 与 `trap on_signal INT TERM HUP`，全部在任何 `stop` 之前装好。`on_exit` 只在 `TIMER_STOPPED=1` 时恢复 timer，并且只恢复一次（`RESTORED` 守卫）。
  2. `is-active <timer>` 判定：输出为 `active` → 进入第 3 步；输出为 `inactive`/`failed`/`unknown` → 不碰 timer，写 `timer_left_inactive`；其余情况（命令失败且输出为空或不认识）→ stderr 写 `timer_state_undeterminable`，exit 2，不运行 python。
  3. 紧挨着 `stop <timer>` 之前置 `TIMER_STOPPED=1`。这样 stop 半途收到信号，也会走恢复。`stop` 本身失败时，stderr 写 `timer_stop_failed rc=<rc>`，exit 2；trap 仍会尝试 start，因为 `TIMER_STOPPED` 已置位。`stop`/`start` 用 `if ! ...; then` 显式捕获返回码，不依赖 `set -e`；`is-active` 按上面的分类规则处理，不靠返回码。
  4. 轮询 service 的 `is-active`：
     - 输出为 `active|activating|deactivating|reloading` → sleep 后继续；
     - 输出为 `inactive|failed` → 跳出循环；
     - 其余（命令失败且输出为空或不认识）→ stderr 写 `service_state_undeterminable`，exit 2（fail-closed，trap 恢复 timer，python 不运行）；
     - 超过上限 → stderr 写 `timer_wait_timeout`，exit 3。
  5. **python 用 `"$PY" ... &` 启动，记 `PY_PID`，再 `wait "$PY_PID"` 取退出码**，不再前台调用。前台调用时 bash 要等子进程退出才执行 trap，TERM 会被推迟到 python 结束。
     - `on_exit` 的**第一句**是 `rc=$?`，先保存原退出码，**然后**才 `set +e`。反过来写的话，`set +e` 会把 `$?` 覆盖成 0，python 退出 7 就会被吞成 0。恢复失败时按这个 `rc` 定退出码：原来是 0 就改成 4，否则保留原值。
     - `on_signal` 和 `on_exit`（在保存 `rc` 之后）都先 `set +e`（handler 内部的 `kill`、`wait`、stderr 写入都可能失败，不能让失败中止 handler）。`on_exit` 里 `start <timer>` 排在所有 stderr 写入之前：HUP 之后终端可能已关闭，stderr 写入会失败，但 timer 必须先恢复。
     - `on_signal` 只在 `PY_PID` 非空时转发信号。等待阶段收到信号时 python 还没启动，直接以 128+信号号退出，EXIT trap 恢复 timer。
     - `on_signal` 收到 INT/TERM/HUP 时：先把同一个信号转发给 `$PY_PID`（非交互 bash 的后台子进程会忽略 SIGINT，所以 INT 要改为发 TERM），再 `wait "$PY_PID"`，然后以 128+信号号退出（INT=130、TERM=143、HUP=129）；EXIT trap 负责恢复 timer。
     - 正常结束时，以 python 的退出码退出。
- 恢复：`start <timer>`。恢复失败时 stderr 写 `timer_restore_failed`，退出码不为 0（python 原本为 0 时改成 4）；同时在 stderr 打印人工恢复命令 `systemctl --user start nhms-compute-scheduler.timer`。
- 所有 stderr 事件日志统一走一个不致命的 helper，例如 `log(){ printf 'node22-run-cycle-once: %s %s\n' "$(date -u +%FT%TZ)" "$*" >&2 || true; }`。主流程和 handler 都用它。stderr 已关闭（`2>&-`，例如 ssh 断线后）时写入失败只会被忽略，不会经 `set -e` 让主流程中途退出。
- 每步向 stderr 写一行 `node22-run-cycle-once: <UTC ts> <event>`，event 取 `timer_stopped`、`service_inactive waited=<s>`、`timer_restored`、`timer_left_inactive`。stdout 只留给 python 输出，因为现有调用方会解析 stdout 最后一行的 JSON（`tests/test_production_scheduler.py:15061`）。

Residual risk（不能 trap 的路径）：SIGKILL、OOM、断电或节点重启会让 timer 一直停着。兜底是 stall probe（`nhms-node22-scheduler-stall-health`，每 15 分钟一次，报 `timer_stopped` 并触发 OnFailure 告警）；人工恢复命令是 `systemctl --user start nhms-compute-scheduler.timer`，写进 runbook。runbook 同时要求长时间运行用 `setsid nohup` 脱离终端，从而避开 ssh 断线。

Governing invariant: 本次运行停掉的 timer，在 wrapper 结束时一定被恢复，不论正常退出、python 失败、等待超时还是收到 INT/TERM；timer 原本没开时，wrapper 不会启动它。secret 的值不出现在 stdout/stderr 中。

Sibling surfaces:
- systemd drop-in 的 `EnvironmentFile`：secret 路径默认值必须与它一致，由测试读 tracked 文件钉住。
- `20-runtime-path.conf` 的 `NHMS_PYTHON_VENV_BIN`：同上。
- runbook 中三处调用方的说明要同步。
- `infra/systemd/node22/MANIFEST.json`：不改，unit 不变。
- Python 侧 scheduler 锁：不改，wrapper 只是避免与它冲突。
- stall probe（`scripts/node22_scheduler_stall_health.py:1662`）：判据是 timer 不 active 且 service 不在跑，即 `timer_stopped`，exit 1 并触发告警。wrapper 运行 python 期间正好是这个状态，所以告警属预期；这一点写进 `stuck-detection.md` 的 `timer_stopped` 处置。代码不改。
- `scripts/model_succession/scheduler.py`：也会 stop/start 同一个 timer。两者的互斥不在本次范围内（见 Non-goals）：succession 每一步都会复查 timer 和 service 都没在跑，发现调度器被中途启动就拒绝，属于 fail-closed。

Seams under test: 以 `bash scripts/ops/node22-run-cycle-once.sh` 为黑盒，在子进程里注入假 python（`$REPO/.venv/bin/python`）、假 systemctl，以及临时 secret / scheduler env 文件。假 systemctl 记录每次调用，按状态文件回答，并**模拟真实返回码**：`is-active` 只有 `active`/`reloading` 返回 0，其余状态返回 3。timer 判定和 service 轮询的所有用例都用这个假 systemctl。现有两个测试补环境注入：假 systemctl（timer 报 inactive）；`--submit` 那个参数化用例再加一个临时 secret 文件路径。

Required evidence（假 systemctl + 假 python）：
- timer active，service 依次报 `activating` → `inactive`：调用顺序为 stop timer → 轮询 → python 运行 → start timer；exit 0。
- timer inactive：既不 stop 也不 start，python 照常运行。
- service 一直 `activating`，等待上限设为 1s：exit 3，stderr 含 `timer_wait_timeout`，timer 被 start，python 没有运行。
- python 退出 7：timer 被 start，wrapper exit 7。
- python 运行期间 wrapper 收到 TERM：假 python 进程被终止（测试结束时它的 pid 已不存在，没有孤儿进程），timer 被 start，wrapper exit 143。
- python 运行期间 wrapper 收到 INT：同上，wrapper exit 130。
- python 运行期间 wrapper 收到 HUP，并且 wrapper 的 stderr 已关闭（`2>&-`）：timer 仍被 start（以假 systemctl 的调用记录为准），wrapper exit 129。
- 轮询阶段（service 持续报 `activating`）收到 INT：python 没有运行，timer 被 start，wrapper exit 130；收到 TERM 时同样如此，wrapper exit 143。
- service 轮询先报 `activating`，再 exit 1 且无输出：wrapper exit 2，python 没有运行，timer 被 start。
- `stop <timer>` 失败（假 systemctl 对 stop 返回 1）：wrapper exit 2，stderr 含 `timer_stop_failed`，python 没有运行，trap 尝试了 start。
- `--submit` 且 secret 路径不存在：exit 2，systemctl 没有被调用，python 没有运行；`--plan` 且 secret 不存在：stderr 告警，python 运行。
- secret 文件中有 `SLURM_GATEWAY_SERVICE_TOKEN=sentinel-xyz`：假 python 能在环境中看到该值；wrapper 的 stdout/stderr 中不出现 `sentinel-xyz`。
- `is-active` 无法判定（假 systemctl exit 1 且无输出）：exit 2，python 没有运行。
- 默认 secret 路径与 tracked drop-in 的第二个 `EnvironmentFile` 相同；`NHMS_PYTHON_VENV_BIN` 默认值与 `20-runtime-path.conf` 同构（`$REPO/.venv/bin`）。

Non-goals:
- 不改 Python 侧锁语义，不改 unit 与 timer 定义；
- 不处理两个手工 wrapper 并发运行（scheduler 锁已能拒绝第二个，返回 lock_contended）；
- 不处理 wrapper 与 model succession 的互斥（succession 每步复查，发现调度器被中途启动就拒绝）；
- 不修改 stall probe：wrapper 持有 timer 期间的 `timer_stopped` 告警属预期，靠 runbook 说明；
- 不轮换 secret。

Review focus:
1. trap 先于 stop 装好；在所有退出路径（含 INT/TERM/HUP、stop 失败、service 状态无法判定）上都恢复且只恢复一次；python 用 `&` + `wait` 启动，信号转发给它；`set -e` 不会绕过 trap；
2. secret 不出现在 xtrace、日志或错误信息中；stderr 已关闭时，主流程的日志写入不会致命（log helper）；`on_exit` 先保存 `rc` 再 `set +e`；
3. stdout 契约不变；
4. 现有测试的断言没有被削弱；
5. 超时与无法判定状态时 fail-closed。
