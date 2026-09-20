**分册：前沿停摆告警**

本页是当前生产值守手册的 §10 分册（#1103 拆分，正文逐字保留）。
索引与全部分册入口见 [`../current-production-ops.md`](../current-production-ops.md)。

## 10. 前沿停摆告警（frontier stall alert）

2026-08-12 事故：scheduler pass 在 forcing 阶段被 NFS 锁挂死，unit 恒
`activating` 从未 failed，业务化静默停摆 ~11h 零告警。补的不是"某个 unit 的超时"，
而是"没有任何机制发现业务化不再产出"。`nhms-node27-frontier-alert.timer`
（每 30 分钟，`scripts/node27_frontier_stall_alert.py`）就是这个机制：它不看
Slurm、不看 unit 状态，只看**最终落库产物**——node-22 任何故障类都坍缩成同一个
可观测量：前沿不再推进。

### 10.1 判据（progress-based，不看墙上时钟）

每 tick 对 `hydro.hydro_run` 做一次只读聚合，限 `cycle_time IS NOT NULL` 且
`status IN ('succeeded','parsed','published')`，按 `COALESCE(source_id,'__null_source__')`
取三个 marker：

| marker | 含义 |
|---|---|
| `max(cycle_time)` | 前沿 |
| `count(DISTINCT cycle_time)` | 回填计数 |
| `max(created_at)` | 到达高水位 |

**进度 = 出现新 source，或某 source 的任一 marker 相对持久化基线严格增大**。
关键性质，看告警时别搞错：

- 判据**永不**比对墙上时钟。追欠账时前沿本就落后数天，wall-clock 判据恒误报。
- **减少不算进度**：行转出集合（`failed`/`cancelled`/`superseded`/`pending`）、
  人工 DBA 删除、source 整体消失，都不重置 stall 计时。
- **基线只升不降**（per-source 高水位）：marker 掉下去再回到原值不算"严格增"，
  否则 `succeeded → failed → parsed` 的出入集合会在真实停摆期伪造一次进度。
- 集合内跃迁（`succeeded → parsed → published`，同一行同一 cycle）**不动快照**，
  不是进度也不是异常。

### 10.2 三类告警邮件怎么读

| 邮件 | 触发 | 第一步做什么 |
|---|---|---|
| `frontier-stalled` | 连续 ≥ `NHMS_FRONTIER_STALL_HOURS`（默认 4h）无进度。首触发一封，持续未恢复每 `NHMS_FRONTIER_RESEND_HOURS`（默认 6h）重发一封 | 按 §6"如何判断是否卡住"走：先看 node-22 `squeue` + scheduler unit 是否恒 `activating`（本次事故几何），再看 node-27 autopipe 日志。邮件正文自带 per-source 快照与 `last_change_at`，可直接判断是"全线停"还是"某 source 早就没数据" |
| `frontier-recovered` | 告警期内出现方向性进度，闭环一封 | 只是闭环通知，不需要处置。正文列出触发恢复的 marker 变化，可用于确认真的是新产出而不是人为改库 |
| `monitoring-degraded` | 告警器**自身**降级，两个细类：`state-corrupt`（状态文件损坏/schema 不符/非 UTF-8 字节/深嵌套 JSON，已按当前观测重建基线）、`observability-unavailable`（观测查询连续失败 ≥ `NHMS_FRONTIER_QUERY_FAIL_TICKS`，默认 2 tick = 1h） | 这类邮件说明"你现在看不见前沿了"，与业务是否正常无关。`observability-unavailable` 先查 DB 可达性与只读角色口令；`state-corrupt` 检查 `NHMS_FRONTIER_STATE_PATH` 所在目录是否被别的东西写坏/磁盘满 |

degraded 邮件正文若以 `(retry: ...)` 开头，说明**上一 tick 就已经降级、只是邮件没发出去**
（sendmail 非零退出）：告警被落盘进 state 的 `degraded_pending`，本封是补发。
判读时间线**必须查 `frontier-alert-events.jsonl`**（追加式，每封告警一行）——
receipt 是 latest-only、每 tick 被原子覆写，出事那一 tick 的 receipt 早就没了。
以 JSONL 里的事件序列 + 邮件正文的 `Reason` 为准，别把补发当成新故障。
补发同样受 6h 去重钟约束；只要没发成功就一直挂在 `degraded_pending` 里，不会被丢。

fail-safe 方向是**宁可多报不可漏报**：告警器的任何内部故障只会**提高**告警倾向。
具体地——查询失败**不重置** stall 计时（瞎着也照样按点发 stalled）；状态损坏立即
发降级邮件而不是静默重置计时；sendmail 非零退出**不记**"已告警"，下一 tick 必然
重试；缺 `DATABASE_URL` 或 `NHMS_ALERT_EMAIL_TO` 直接结构化 config error 退出非零
（unit failed 可见），绝不带默认收件人静默跑。状态文件**缺失**是唯一的静默分支：
那是首次安装/换路径的 bootstrap，只记 `baseline_established_at`，代价是人为删档会
伪装成 bootstrap、盲窗 ≤ 一个 stall 窗口。

**持久性损坏**这类误配置有**两种截然不同的表现**，别把它们当成一回事：

| 现场 | 表现 | 怎么发现 |
|---|---|---|
| `NHMS_FRONTIER_STATE_PATH` 指到一个目录（锁能开、state 写不下去） | 去重钟随状态一起丢 → **每 tick 一封** `state-corrupt`（上限 48 封/日），同时每 tick exit 1、unit failed | 邮箱被刷屏；这是刻意选的过报方向，不是 bug |
| **锁文件本身打不开**（卷只读、root 用 sudo 跑过一次留下 root 属主的 `<state>.lock`） | 结构化 config error、**rc=2、零邮件** —— 告警器根本没跑到观测那步 | **只有 `systemctl --user status nhms-node27-frontier-alert.service` 的 failed 状态和 `frontier-alert.log` 能看见**，邮箱一片安静。定期看治理 receipt 里的 unit 状态就是为了这个 |

两种都先修路径/属主本身，别去调告警器参数。第二种只是"故障但零邮件"这一**族**的
**一个**成员：同族还有 wrapper 期的 env 文件是符号链接 / 权限宽于 0600 / 源失败，
以及缺 `DATABASE_URL`、`NHMS_ALERT_EMAIL_TO` 的 config error —— 这些都发生在任何
邮件通道之前，现场证据行是 bootstrap 日志 `/home/nwm/node27-frontier-alert.log` 里的
`BLOCKED rc=2 reason=<REASON>`（如 `ENV_FILE_SYMLINK_FORBIDDEN` /
`ENV_FILE_MODE_UNSAFE`），runner 期成员则写 `<log root>/frontier-alert.log`。

族里还有一个**通道之内**的成员：收件人/发件人取值让邮件正文编不出来（env 里混进
非 UTF-8 字节，进程内表现为代理对）。它每 tick 都走完判定、把发送记成
`returncode=70`（`SEND_INTERNAL_FAILURE_RC`）的失败并 exit 1、unit failed，但
**不会**有 `BLOCKED rc=2` 行；证据在 `frontier-alert-receipt.json` 的
`send_failures` / `emails[].error` 与 `frontier-alert-events.jsonl` 的 `sent:false`
（告警本身不算已送达，下一 tick 继续重试）。全族排查路径相同：unit failed ->
上述日志/receipt -> 治理 receipt 里的 unit 状态；几何不对称的进一步收口另立 issue。

### 10.3 阈值口径（改之前先读这段）

默认 4h 的来源：全趟实测 2h16m–3h01m，加落库滞后 15–30min，健康周期的现实上界
约 3h30m，4h 是留余量后的最小安全值。**别为了"少收几封"随手上调**——阈值就是
"业务停多久才被发现"的上限。真要调，先在 node-27 记录几天的真实 pass 时长再定，
并同步改 `infra/env/node27-frontier-alert.env` 与本节。resend 6h 同理：调大等于
延长"已知停摆但没人再被提醒"的窗口。

**阈值必须大于生产 cycle 节奏**，否则稳态本身就是"无进度"、天天误报：4h 只适配
回填期（一天多个 cycle）；稳态**每天只跑 1 个 cycle** 时，阈值要 ≥ 节奏的 ~2 倍
（漏掉两个日 cycle 才告警）。**生产现值：48h**（operator 裁定 2026-08-14，
`node27-frontier-alert.env` 已改；回填结束进入单日 cycle 稳态的口径）。resend 保持
6h——那是"真停摆持续期间的提醒频率"，与触发阈值是两个旋钮。

### 10.4 误报处置

先确认是不是**真**误报——"前沿 4h 没动"本身几乎总是事实，问题只在于是不是可接受：

1. 计划内停机 / 人工暂停业务化：告警属预期。停机前 `systemctl --user stop
   nhms-node27-frontier-alert.timer`，恢复后再 start；别改阈值。
2. 上游断供导致某个 source 长期不来：判据是**观测级**的（任一 source 推进=业务
   活着），单 source 断供不会触发本告警；若全部 source 同时断供，那不是误报。
3. 怀疑判据本身出问题：`--dry-run` 完整跑一遍判定并打印本应发生的动作，**零副作用**
   （不发邮件、不写 state/receipt/JSONL）：

```bash
ssh -p 32099 nwm@210.77.77.27
cd /home/nwm/NWM
set -a; . infra/env/node27-frontier-alert.env; set +a
.venv/bin/python scripts/node27_frontier_stall_alert.py --once --dry-run | python3 -m json.tool
```

4. 确认业务其实活着而告警仍在：读**当前** receipt 的 `baseline` 与 `observation`
   两块，对比哪个 marker 应该增而没增；要看**历史**（哪一 tick 开始不动、发过几封）
   一律读 `frontier-alert-events.jsonl`——receipt 只有最新一 tick，每 tick 覆写。
   `hydro.hydro_run` 侧用 §9 的值守 SQL 交叉验证。

### 10.5 状态、产物与恢复闭环语义

- 状态：`NHMS_FRONTIER_STATE_PATH`（原子 tmp+rename，含 `schema_version`、
  per-source 高水位基线、`last_change_at`、`alert_active`、`last_alert_at`、
  `baseline_pending`+`baseline_pending_kind`、`degraded_pending`、
  `last_degraded_alert_by_kind`）。单实例互斥是**脚本内** `fcntl.flock`
  （锁文件 `<state>.lock`），第二实例结构化 no-op 退出 0，不会重复观测或重复发信。
  锁目录不可建/不可写（EACCES/EROFS/ENOTDIR）走结构化 config error 退出 2，不是
  裸 traceback。
- **`baseline_pending`（基线待建）**：基线只能由**真实观测**建立。若 bootstrap 或
  损坏重建那一 tick 观测同时失败，脚本**不落空基线、不记** `baseline_established_at`
  / `baseline_reset_at`，只置 `baseline_pending=true`。下一次成功观测会**静默**填充
  基线——**不算进度、不动 `last_change_at`、不清 `alert_active`**。这条很关键：若
  当时把空基线当真，下一次成功观测会把所有 source 判成"新 source"=进度，把 stall
  时钟整整推后一个 DB 中断时长（漏报）。在 receipt 里看到 `baseline_pending: true`
  就意味着"现在没有可比对的基线"，优先修 DB 可达性。填充时按 `baseline_pending_kind`
  记到正确的戳位：损坏起源记 `baseline_reset_at`（如实说"这是一次降级重建"），
  bootstrap 起源记 `baseline_established_at`——损坏重建绝不冒充全新安装。
- 产物：`NHMS_FRONTIER_RECEIPT_PATH`（每 tick 原子覆写，latest 语义）+ 同目录
  `frontier-alert-events.jsonl`（追加式告警事件流）。两者都不是正式 schema
  产物，无跨工具消费方。
- 恢复闭环：`alerting` 状态下一旦出现方向性进度，发**恰一封** `frontier-recovered`
  并清 `alert_active`；此后重新计满一个完整 stall 窗口才可能再次触发，不会因为
  "刚恢复又慢了半小时"连环发信。恢复邮件发送失败不会把告警重新挂起（不存在的
  停摆不该被重新武装），失败事实记在 receipt 与 JSONL 里。
- **发送失败的重试语义**：stalled 邮件发失败 → 不记 `last_alert_at`，下 tick 必重试，
  且补发那封**仍标 `initial`**（它才是本轮真正投出去的第一封，operator 的时间线
  按投递事实重建）。degraded 邮件发失败 → 进 `degraded_pending`，下 tick 在 6h
  去重钟允许时补发，成功即清；补发正文带 `(retry: ...)` 前缀。recovered 邮件发失败
  是唯一不重试的一类（见上）。
- **degraded 去重钟是 per-kind 的**（`last_degraded_alert_by_kind`）：`state-corrupt`
  与 `observability-unavailable` 各有自己的 6h 窗。一封 `state-corrupt` **不会**
  压掉紧随其后的首封 `observability-unavailable`——那等于整类事件从未被告知
  （漏报方向）。同类 6h 内的重复仍然去重。

### 10.6 投递通道（认证 SMTP shim，不要用本机 sendmail）

告警器只会 `<NHMS_FRONTIER_SENDMAIL> -t -i`（正文走 stdin，exit 0 == 已发），
它自己不认识 SMTP。**node-27 上这个路径必须指向
`scripts/node27_frontier_smtp_sendmail.py`**（stdlib-only shim，直接以
`smtplib.SMTP_SSL` + 认证向 `smtp.163.com:465` 投递），凭据只从
`NHMS_SMTP_HOST/PORT/USER/PASS` 读，绝不走 argv，`NHMS_SMTP_PASS` 只喂
`login()`、不进任何输出。

**不要用默认的 `/usr/sbin/sendmail`**：node-27 的本机 postfix 是刻意断路的
（`default_transport = error`，只听 loopback）。它照单全收、exit 0，然后**异步**
把所有外发信退掉——2026-08-13 实测 `dsn=5.0.0 status=bounced`。即"exit 0 但从未
投出"，告警器会把它记成一封成功的 stalled 邮件。

shim 的退出码即投递事实：0 = 目的地提交服务器**同步**回了 250（stderr 留一行
`SMTP-ACCEPTED host=... code=250 recipients=<n>`）；69 = 投递失败
（`SMTP-FAILED stage=connect|ehlo|login|send ...`，其中会话预算到期那条带
`reason=session-budget elapsed=<s> budget=<s>`，或部分收件人被拒的
`SMTP-PARTIAL-REFUSAL ...`）；64 = 配置/用法错（缺 `NHMS_SMTP_USER` /
`NHMS_SMTP_PASS`、端口非法、`NHMS_SMTP_SESSION_BUDGET_SEC` 非正数或 ≥60s、正文无
收件人、From 与认证账号不一致）；70 = 内部故障
兜底（整类 contained，永不吐 traceback）。全部非零都会被告警器当作发送失败记进
receipt 与 JSONL，下一 tick 按 §10.5 的重试语义重来。TLS 是**验证过的**
`ssl.create_default_context()`——`SMTP_SSL` 的缺省 context 是 `CERT_NONE` + 不校验
主机名，那样授权码会递给任何应答方、250 也可被路径上伪造。

**三层时限，各管各的**（不是"嵌套超时"，三个值的量纲不同）：

1. **单次操作 30s**（`SMTP_TIMEOUT_SEC`，socket 级）：每次阻塞读写各自计时，**会被
   重置**。一次会话有 ≥8 次往返（connect/TLS/greeting/ehlo/login/MAIL/RCPT/DATA/
   收尾点），所以它**不是**会话上限；对方每 29s 挤一个字节就能把它无限续期。
2. **会话预算 45s**（`SESSION_BUDGET_SEC`，`setitimer(ITIMER_REAL)` 一次性闹钟）：
   从连接前起算的墙钟，到期就在**当前 stage 上**中断正在阻塞的那次调用，打
   `SMTP-FAILED stage=<stage> ... reason=session-budget` 并退 69，随后用
   `close()` 而不是 `quit()` 拆链路（不再多一次往返）。要改用
   `NHMS_SMTP_SESSION_BUDGET_SEC`（正数秒）——这是与下面那道 60s 墙**唯一的对齐
   点**。该 env **必须严格小于 60s**：`SESSION_BUDGET_CEILING_SEC = 60.0` 把
   ≥60 的值直接判成配置错（rc=64，连都不连），否则 SIGKILL 先到、stage 又丢，
   等于把改动前的几何悄悄装回来。默认值与这道天花板都有跨模块测试盯着
   （shim 不 import 告警器：它是被绝对路径 exec 的，依赖方向是 lane → shim，
   常数是镜像的，测试就是校准点）。
3. **告警器 60s 墙**（`SENDMAIL_TIMEOUT_SEC`，`subprocess` SIGKILL）：只是兜底，
   正常情况轮不到它——第 2 层先退。

**"没收到 250" ≠ "没投出去"**。RFC 5321 的收尾点一旦被服务器接收，投递责任就已
转移；我们只是没等到那个 250。所以下面**三种**记录都要按"**信可能已经投出去**"读：

- `rc=124` 且 receipt 里**没有** `SMTP-FAILED stage=` 行 —— **shim 自己挂死了**
  （不是投递失败，投递失败会带 stage），连自己那行都没来得及打；
- `rc=69` + `SMTP-FAILED stage=send ... reason=session-budget` —— 会话预算在
  DATA 中途开了闸；
- `rc=69` + `SMTP-FAILED stage=send ... error=TimeoutError`（或别的 socket 超时）
  —— 同一个窗口，只是这次是单次操作先超时。

其余 stage（`connect`/`ehlo`/`login`）的失败**确实**证明没投出去。三种"可能已投"
的情况下，下一 tick 都会按设计重发（§10.5 的重试语义），operator 可能因此收到一封
重复告警——这是**刻意选的**过报方向，不要按"重复发信"去查 bug。receipt 的 `error`
里若带 `| sendmail stderr: ...` 尾巴，那是 shim 被 kill 前来得及打印的内容，优先按
它定位。

163 会拒绝 From 头与认证账号不一致的信，而 shim **不改** From 头——所以
`NHMS_ALERT_EMAIL_FROM` 的**地址部分必须等于** `NHMS_SMTP_USER`（display-name
形式 `NHMS Frontier Alert <账号地址>` 允许且推荐，比对只看 `<>` 里的地址；见
`.example`）。不一致时 shim 在**连接之前**就退 64 并把两个地址都打印出来，不会每
tick 去换一个 550。

**成功也留证**：shim 那行 `SMTP-ACCEPTED ...` 会被告警器捕获进 receipt 与 JSONL 的
`emails[].evidence`（DSN 打码后）。receipt 里 `sent=true` 而 `evidence` 为 `null`，
说明发信通道不是本 shim（经典 sendmail 成功时不打印任何东西）——那正是 §10.7 那条
"exit 0 什么都不证明"的几何。

**正文含中文**（runbook 指引），所以 shim 会先 `EHLO`：服务器宣告 `8BITMIME` 就带
`BODY=8BITMIME` 发原始 UTF-8，否则把正文重编码成 quoted-printable，**绝不**裸推
8-bit（严格服务器会拒、宽松服务器会砍高位，把唯一一封告警变成乱码）。信头里的非
ASCII（例如 From 的中文 display name）统一按 `=?utf-8?...?=` 出线，不会出现无人能
渲染的 `=?unknown-8bit?...?=`。

**不要在 `NHMS_ALERT_EMAIL_TO` 里配多个收件人**：一封信一个信封，只要有一个地址被
拒，整次投递就算失败（exit 69）——已经收到的那位会在停摆期间每 30 min 收到一封重复
告警。要分发就在邮箱侧做别名/转发，别在这里堆地址。

### 10.7 信号口径（认领的盲区，不是 bug）

本 lane 观测的是 **post-ingest 前沿**（含 `succeeded`），**不是**严格的
display-published 前沿。即：ingest 仍在落库、但 parse/publish 段静默冻结的几何
**不会**触发本告警。这是刻意取舍——该故障类会让 autopipeline 非零退出、unit
failed，属于 systemd 已经能看见的面，与本 issue 针对的"挂死但从不 failed"不同类。
要观测 display 已发布前沿，另立 issue，别把判据混进本 lane。

同样不在本 lane 范围：`RuntimeMaxUSec` 之类 unit 超时兜底、钉钉/企业微信通道、
per-source 独立告警、以及任何自动恢复动作（本 lane 只通知不处置）。

**投递侧那条盲区已经不是理论问题，也已经收口**（2026-08-13 live receipt）：
告警器只认 sendmail 的退出码，而本机 postfix 的 exit 0 只代表"本机收下了"。实测
它随后异步 bounce（`dsn=5.0.0 status=bounced relay=none`，`default_transport = error`）
——**投递失败而告警器记成成功**，是本 lane 唯一漏报方向的几何。改用 §10.6 的认证
SMTP shim 后该盲区闭合：250 由目的方提交服务器**同步**返回才退 0，并留下
`SMTP-ACCEPTED` 证据行。**若把 `NHMS_FRONTIER_SENDMAIL` 改回 `/usr/sbin/sendmail`，
盲区原样复现。**

### 10.8 安装

```bash
ssh -p 32099 nwm@210.77.77.27
cd /home/nwm/NWM
cp infra/env/node27-frontier-alert.example infra/env/node27-frontier-alert.env
chmod 600 infra/env/node27-frontier-alert.env
# 填入只读角色 DSN（nhms_display_ro）、收件人（**只填一个**，多收件人的部分拒收会
# 让已收方每 30 min 重复收信，见 §10.6），以及 §10.6 的 NHMS_SMTP_USER /
# NHMS_SMTP_PASS（163 授权码，手工存入、绝不入库）；NHMS_ALERT_EMAIL_FROM 的地址
# 部分必须等于 NHMS_SMTP_USER（不一致 shim 退 64，零投递）。
# wrapper 在**任何**路径（systemd 或
# 手工）都拒绝符号链接 / 非 0600 的 env 文件——权限契约与"谁来 source"无关。
bash -n infra/env/node27-frontier-alert.env   # 填完先过一遍语法（见下 env 语法警示）
install -m 644 infra/systemd/nhms-node27-frontier-alert.service ~/.config/systemd/user/
install -m 644 infra/systemd/nhms-node27-frontier-alert.timer   ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now nhms-node27-frontier-alert.timer
systemctl --user list-timers 'nhms-node27-frontier-alert.timer' --no-pager
tail -n 50 /home/nwm/node27-frontier-alert-logs/frontier-alert.log
```

首 tick 是 bootstrap：**预期零邮件**，receipt 记 `baseline_established_at`。要验证
真实投递，把 state 里的 `last_change_at` 人为回拨 5h，下一 tick 会真发一封
stalled；投递验证必须三重——shim exit 0 + receipt/JSONL 里 `emails[].evidence` 的
`SMTP-ACCEPTED ... code=250`（250 来自 smtp.163.com 提交服务器，同步；`evidence`
为 `null` 说明根本没走这个 shim）+ 收件箱人工确认，**不得只看 exit 0**
（用本机 sendmail 时 exit 0 什么都不证明，见 §10.7）。unit 已注册进 `scripts/node27_resource_governance.py`
`DEFAULT_SERVICES`，治理审计 receipt 里能看到它的 systemd 状态，含 `LoadState` /
`UnitFileState`（逐状态读法见 §11.5；本车道的 service 同样没有 `[Install]` 段，读 `static`
是常态）：timer 被人 disable 掉时，**按周期读 receipt** 能认出来，而不必从"怎么没收到邮件"去猜。
但这**不是自动发现**：治理审计不对 receipt 的 `systemd` 段产任何建议、不因此非零退出、也不发
邮件——没人读 receipt，就没人知道 timer 停了。

**env 文件单读者 + 双语法警示**：systemd 路径下 env 由 service 的
`EnvironmentFile=` 读**一次**，同时 service 注入 lane 专属哨兵
`Environment=NODE27_FRONTIER_ALERT_ENV_INJECTED=1`；wrapper 只在该哨兵**缺席**
（手工 shell 调试）时才 `source`，不会二次解析。哨兵刻意不用 `DATABASE_URL`——那是
全仓最常被别的 lane 导出的变量名，用它当哨兵会让调试 shell 里带着他 lane DSN 的人
静默跳过 source、跑错库。注意：**符号链接 / 0600 校验不受哨兵约束，恒执行**。
但同一份文件仍可能被两种语法读到，所以：含空格
的值**必须加引号**（systemd 与 bash 都会剥掉外层双引号），密码里出现
`` $ ` " \ `` 时两个解析器**释义不同**（bash 在双引号内会展开/吞掉，systemd 不会）
——这种口令要么换成 `[A-Za-z0-9._~-]` 字符集，要么两条路径都实测一遍。改完
`.env` 先跑 `bash -n`，再 `systemctl --user restart`。

**tick 有执行期限**：service 用 `TimeoutStartSec=900`（不是同族的 `0`）。oneshot
的整个执行就是 start 阶段，所以这是唯一生效的期限；正常 tick 是秒级（connect 10s +
statement 30s + sendmail 60s 上限），900s 只兜挂死。**挂死的监控器必须变成 unit
failed**——否则就是 2026-08-12 那套"恒 activating、零告警"的几何在监控层重演。
