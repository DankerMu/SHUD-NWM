# Tasks: node22-cycle-once-timer-and-secret (#3113)

Fixture level: expanded
Must preserve：见 design.md（CLI、参数顺序、repair flag 语义、stdout 契约、现有测试断言）。

## Risk packs
- Public API / CLI / script entry — selected：参数与 stdout 契约不变（现有测试 + 1.4 的 stdout 断言）
- Auth / permissions / secrets — selected：secret 加载但不泄漏（1.4 的 sentinel 用例）；submit 缺 secret 时 fail-closed
- Concurrency / shared state / ordering — selected：timer 停、等、恢复的顺序（1.4 的调用序列断言）
- Error handling / rollback / partial outputs — selected：超时、python 失败、INT/TERM/HUP/QUIT、stop 失败、service 状态无法判定时都恢复 timer（1.4）；SIGKILL 是 residual risk，靠 stall probe 兜底（design.md）
- Config / project setup — selected：默认路径与 tracked drop-in 一致（1.4 的 drift 用例）
- Documentation / migration notes — selected：runbook 三处同步（1.5）
- File IO / path safety — not selected：只读 secret 文件，不写文件
- Legacy compatibility / examples — selected：现有两个 wrapper 测试的断言不变（1.4）；runbook 中现有调用示例保持可用（1.5）
- Resource limits — selected：4h 等待上限和轮询间隔是资源边界决策，对应 1.4 的超时用例
- Schema / Release — not selected：不涉及

## 1. 实现
- [x] 1.1 wrapper 加载 secret env（`NHMS_NODE22_GATEWAY_SECRET_ENV`），导出 `NHMS_PYTHON_VENV_BIN`；`--submit` 缺 secret 时 exit 2，`--plan` 缺 secret 时只告警
- [x] 1.2 timer 协调，按 design.md 的步骤：先装 trap（EXIT/INT/TERM/HUP/QUIT）→ 判定 timer 状态（无法判定则 exit 2）→ 置位后 stop（stop 失败则 exit 2）→ 有上限地轮询 service（无法判定则 exit 2，超时则 exit 3）→ python 以 `&` 启动并 `wait`，信号转发给它，INT/TERM/HUP/QUIT 分别以 130/143/129 退出 → 恢复 timer（只恢复一次；恢复失败时退出码为 4，并打印人工恢复命令）
- [x] 1.3 stderr 事件日志；stdout 只给 python
- [x] 1.4 测试（`test_node22_exact_cycle_wrapper_*`）：design.md "Required evidence" 中的全部用例；假 systemctl 模拟真实 `is-active` 返回码（`activating` 返回 3）；现有两个测试只补环境注入
- [x] 1.5 runbook：
  - `known-issues-pipeline.md` 的 exact-cycle 小节：
    - 说明 wrapper 会自动暂停和恢复 timer、加载 secret，以及等待上限和各退出码；
    - 执行期间不要手动启动 timer；
    - 长时间运行用 `setsid nohup ... > log 2>&1 &` 脱离终端；
    - SIGKILL、OOM 后用 `systemctl --user start nhms-compute-scheduler.timer` 人工恢复。
  - `file-provider-refresh.md` 约 :992：重写"该脚本 source `infra/env/compute.scheduler-dbfree.env`"这句，改为说明它加载的三项环境（scheduler env、gateway secret、`NHMS_PYTHON_VENV_BIN`）以及 timer 的暂停和恢复。
  - `node22-control-plane-manual-recovery.md` 约 :420：加同样的简要说明。
  - `stuck-detection.md` 约 :217-221 的 `timer_stopped` 处置：照 `recalibration-and-archive.md:243` 的先例，写明"wrapper 持有 timer 期间报 `timer_stopped` 属预期，start 之前先确认没有 wrapper 在跑（`pgrep -f node22-run-cycle-once`）"。

## 2. 上线（合并后）
- [x] 2.1 node-22 先查 `git status --porcelain`，再 `git pull --ff-only`（禁止 `uv sync`、裸 `uv run`）
- [x] 2.2 对一个已完成的 cycle 实跑 `--plan`。日志应依次出现 `timer_stopped` → `service_inactive` → `timer_restored`；状态不是 `lock_contended`；跑完后 timer 为 active。日志贴到 #3113

## Evidence Floor
- `uv run pytest -q tests/test_production_scheduler.py -k node22_exact_cycle_wrapper`，加上 selector 选出的测试集
- `uv run ruff check .`
- 2.2 的 node-22 日志

## 上线记录（2026-11-10）
- 2.1：node-22 部署到 `887914edb`。
- 2.2：线上 `--plan`（cycle 2026-10-24T00）：
  - 事件依次为 `timer_stopped`（17:42:28Z）→ `service_inactive waited=9445` → `timer_restored`（20:33:08Z）；
  - plan 结果为 `planned` / `planning_only`，没有 `lock_contended`；
  - 结束后 timer 为 active。
- 证据：#3113 issuecomment-6779267938。
