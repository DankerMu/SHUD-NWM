**分册：覆盖新鲜度告警**

本页是当前生产值守手册的 §11 分册（#1103 拆分，正文逐字保留）。
索引与全部分册入口见 [`../current-production-ops.md`](../current-production-ops.md)。

## 11. 覆盖新鲜度告警（coverage freshness alert）

`hydro.run_display_coverage` 在 #2080 之前**全仓没有任何新鲜度观察者**：两处
coverage 刷新调用点都是 non-fatal、autopipe unit 没挂 `OnFailure=`，而 §10 那条
车道只读 `hydro.hydro_run`——coverage 停摆期间 ingest 照常前进，它的方向性进展判
据每 tick 都被重置，**按构造恒不告警**。#2009 引入 `NATIONAL_DISCHARGE_CYCLE_LOOKBACK_DAYS`
回看窗之后，coverage 停摆不再降级成"陈旧但在线"：被覆盖的 cycle 老出窗口，全国流量
图层直接熄灭（`/api/v1/layers` 的 `default_cycle=null` + `valid_times=[]`）。
`nhms-node27-coverage-freshness-alert.timer`（每天 06:00，
`scripts/node27_coverage_freshness_alert.py`）补的就是这个探测器。

### 11.1 判据（两个前沿的关系型 gap，不是 rc、不是墙上时钟）

逐 source key `COALESCE(lower(source_id),'__null_source__')` 比两个前沿：

| 前沿 | 取法 |
|---|---|
| **ready**（ingest 已产出、可展示） | 车道自己那条唯一重导语句：`hydro.hydro_run` 限 `status IN ('succeeded','parsed','published')` + `cycle_time IS NOT NULL`，JOIN `core.model_instance`（`active_flag` 且 `river_network_version_id IS NOT NULL`）后取 `max(cycle_time)` |
| **covered**（全国目录当前列出的最新 cycle） | **直接调 `services/tiles/mvt.py` 的 `national_discharge_cycles(session, source=<key>)` 取 `default_cycle`**，一个谓词都不自己重导 |

covered 侧**按构造与被观测面同一**（设计 D0）：`default_cycle` 就是
`/api/v1/layers` 发布的那个字段、就是图层熄灭时变 `null` 的那个字段。曾经的初稿用
`rdc.segment_count > 0` 重导覆盖谓词，**那比真正点亮图层的条件弱**——目录还会因为
覆盖窗列不一致（`river_valid_time_start/end`、`min/max_lead_time_hours` 为 NULL，
`river_sample_count != segment_count * lead_count`，跨度不等于 `(lead_count-1)*3600` s）、
小时网格与 cycle 的 3h 相位对不上、或活跃网络集合不完整而丢掉该 cycle。用重导谓词
会在图层已经黑掉时报 `gap = 0`。**别再把 covered 侧改成自己查表。**

判读要点：

- **gap 超阈 → 退出 1 并发信**。阈值默认 `NATIONAL_DISCHARGE_CYCLE_LOOKBACK_DAYS / 3`
  （当前窗口 12 天 → 4.0 天），不写死天数，窗口调小阈值自动跟着小。
- **判据是结果型（outcome-based），不看 rc**。`scripts/node27_autopipeline.py` 有
  合法的 rc=0 `no_coverage_row` 路径；扫描因上游原因恒返回零行时，所有 rc 通道全程静默。
- **只有一条墙上时钟规则**：source 的 ready 前沿早于 `now() - 回看窗` 时记
  `not-evaluated`，不参与告警——这种 source 本来就不进全国图层，属于 ingest/保留
  问题。`__null_source__` **恒** `not-evaluated`：目录按 `lower(h.source_id) = :source`
  匹配，NULL-source 的 run 根本不可能被逐 source 目录列出。
- **只评估展示面实际服务的 source（#2464）**：source key 是从 ingest 表发现的（开放集），
  而被观测的展示面是 display 路由 `source` 参数的闭集——`packages/common/source_identity.py`
  的 `DisplaySourceId` / `DISPLAY_SOURCE_IDS`（今天 `gfs`、`ifs`），三条路由与本车道读同一个别名。
  闭集之外的 key（例如已在编排里接线的 ERA5，一旦运维注册了新 data source）记
  `not-evaluated` + `reason=unsupported-source`：行照常打印、**不进退出码、不发信**。
  这**不是**覆盖问题，没有任何 coverage 动作能、也需要"清掉"它；它该不该上全国图层是产品决策，
  不在本车道。

`not-evaluated` 的 `reason` 一览（均为纯说明性，不进退出码）：

| `reason` | 含义 | 处置 |
|---|---|---|
| `null-source` | `source_id IS NULL` 的 run（`__null_source__`），逐 source 目录无法列出 | 无 |
| `unsupported-source` | source 不在 display 路由的 `source` 枚举内（#2464） | 无；若确要上展示面，是改 `DisplaySourceId` 的产品/契约决策 |
| `outside-window` | ready 前沿早于 `now() - 回看窗` | 归 §10 前沿车道 |
| `no-ready-frontier` | 防御分支（按 ready 前沿语句构造不可达） | 留证开 issue |

- **与 §10 的分工**：ingest 全线停摆时两个前沿一起冻住，gap 不增长，**本车道沉默**，
  那是 `frontier-stalled` 的活；coverage 停摆只推进 ready 前沿，gap 单调增长，在图层
  熄灭前数天就跳闸。

### 11.2 退出码与邮件怎么读

| 退出码 | 含义 | 第一步做什么 |
|---|---|---|
| `0` | 所有已评估 source 都在阈值内（表照常打印） | 无需处置 |
| `1` | 至少一个已评估 source `gap-exceeded` 或 `no-covered-cycle` | 走 §11.3 三个分支 |
| `2` | 配置错误，`code` 为 `COVERAGE_FRESHNESS_CONFIG_INVALID`（`DATABASE_URL` 缺失、阈值非法、**导入期**展示模块报错——venv 里没有展示栈、展示模块的依赖漂移、或仓库代码本身坏了），**观测前**就退出。unit 缺 `Environment=PYTHONPATH=` 在今天的 node-27 上**不是**退 2 的原因：venv 以 editable 方式装了本项目，不设 `PYTHONPATH` 照样导得进（2026-09-18 实测，见 §11.5） | 先看 stderr 那行结构化 JSON 的 `reason` 是不是以 `<异常类>: …` 开头。**是** → 导入期：`main` 在配置阶段给 `CoverageAlertConfigError` 以外的**任何**异常都冠上类名，而 `config_from_env` 里唯一不带守卫的调用就是 `lookback_days()`（延迟 `import services.tiles.mvt` + 读常量）。最常见的是 `ImportError:` / `ModuleNotFoundError:`，但别的类（例如仓库代码坏了时的 `SyntaxError:`、依赖漂移时展示模块体里抛的 `AttributeError:`）也是同一期。冒号后若是 `<error text withheld: redaction unavailable (…)>`，同样是导入期，只是车道连脱敏模块都导不进、把原文扣下了（node-27 上要 venv 的 editable install 也没了才会在退 2 出现，推导）。按 §11.5 的「导入期失败」那段先做导入检查，再按检查打印的最后一行分四支（仓库代码缺陷、缺第三方依赖、已装的第三方包与代码对不上、检查通过），**不是** §11.4 的阈值旋钮。**不是**（没有类名前缀，例如 `DATABASE_URL must be set`、`NHMS_COVERAGE_GAP_DAYS must be a number, got 'abc'`）→ 配置本身，去 §11.4 改 env 文件。`ImportError:`、`ModuleNotFoundError:`、`SyntaxError:`、`AttributeError:` 四种前缀车道退 2：2026-09-18 在 node-27 的 scratch worktree 里用生产解释器实测（`AttributeError` 那一例是 `AttributeError: module 'sqlalchemy' has no attribute 'no_such_attr_2472'`），单测另钉住 `AttributeError` 前缀；两种配置错误无前缀由单测钉住（读 `scripts/node27_coverage_freshness_alert.py` 的 `main` 推导）；`withheld` 那一形状是推导 |
| `3` | 观测失败，两个 `code`：`COVERAGE_FRESHNESS_OBSERVATION_FAILED`（DB 不可达 / statement 超时 / 权限拒绝 / 连上的库里没有本车道的表 / **观测期**展示模块报错）与 `COVERAGE_FRESHNESS_NO_SOURCES`（ready 前沿查询一个 source key 都没返回） | 先看 stderr 那行结构化 JSON 的 `code`：`COVERAGE_FRESHNESS_NO_SOURCES` → §11.3 最后一条（「什么都观测不到」）；`COVERAGE_FRESHNESS_OBSERVATION_FAILED` → 按下面的「退 3 路由」读 `reason` |

**退 3 路由（`COVERAGE_FRESHNESS_OBSERVATION_FAILED`）**：`reason` 的形状是
`<SQLAlchemy 异常类>: (<驱动异常类>) <驱动原文>`，驱动没参与时没有括号那一段。只看冒号前的
SQLAlchemy 类**分不开**原因——`OperationalError` 既是连不上也是 statement 超时，
`ProgrammingError` 既是权限拒绝也是连错了库。所以先看 `reason` 里有没有 `(psycopg2.`，有就按
括号里的驱动类分。下表**自上而下、第一条命中即停**：具名驱动类优先于兜底行。`VERDICT: FAIL`
那行带的是同一段 `reason`（拍平成一行、超长截断），邮件尾巴里只剩它时照样按它分。

| `reason` 开头 | 含义 | 第一步 | 依据 |
|---|---|---|---|
| `OperationalError: (psycopg2.errors.QueryCanceled)` | statement 超时（车道自设 `statement_timeout` 30 s） | §11.3「库侧观测失败」第 3 步（`pg_stat_activity`） | 推导：`QueryCanceled` 是 `psycopg2.OperationalError` 的子类 |
| `ProgrammingError: (psycopg2.errors.InsufficientPrivilege)` | 只读角色缺 grant | §11.3「库侧观测失败」第 4 步（grant） | 推导：`InsufficientPrivilege` 是 `psycopg2.ProgrammingError` 的子类 |
| `ProgrammingError: (psycopg2.errors.Undefined`…（`UndefinedTable`、`UndefinedColumn`、`UndefinedFunction`…） | 连上的库里没有本车道的表（DSN 指错了库），或 schema 漂移（部署的代码期待的表 / 列 / 函数库里没有） | §11.3「库侧观测失败」第 2 步：先查 unit 读哪份 env 文件，再用探针分——探针报出库连错了 → 改 DSN；探针健康 → 那一步里 schema 漂移那一支 | `UndefinedTable`（连错库）实测；schema 漂移那一支推导 |
| 其余任何带 `(psycopg2.` 的 | 基类 `psycopg2.OperationalError`（常见三种原文与兜底见下）、`psycopg2.errors.AdminShutdown`（观测中途容器重启）、`InterfaceError`、死锁 / 锁等待类错误… | §11.3「库侧观测失败」，从第 1 步（容器状态）做起 | 基类常见三种原文实测；其余推导 |
| 不带 `(psycopg2.` | 根本没走到驱动，或不是数据库错误 | 按下面「不带驱动类」四条分 | 见各条 |

基类 `(psycopg2.OperationalError)` 后面的原文**常见三种**，2026-09-18 在 node-27 上逐一实测：
`Connection refused`（库没在听：容器停了 / 端口不对）、`password authentication failed`（口令错）、
`database "…" does not exist`（DSN 里的库名错）。**这三种不是穷举**：车道连库带
`connect_timeout=10`，容器挂死或网络不通时读 `timeout expired`；连接数打满读
`sorry, too many clients already`；容器正在起读 `the database system is starting up`。三种之外的
**任何**原文都走同一条兜底：库在、但没在接受或服务连接 → 先 §11.3「库侧观测失败」第 1 步（容器
状态），按那里的读法走到第 2 步的探针；探针也以这类原文失败，再第 3 步（`pg_stat_activity`）/ §9.2 看是谁
占着，探针正常则是已经过去的瞬时故障。兜底这条是推导，未在 node-27 实测。
一封真实的（已脱敏）`reason`：

```text
OperationalError: (psycopg2.OperationalError) connection to server at "127.0.0.1", port 55432 failed: FATAL:  password authentication failed for user "***"
```

角色名是 `***`：车道会把与 DSN 用户名相同的角色名脱敏，别指望从邮件里看出是哪个角色——看
§11.4 那份 env 文件里 `DATABASE_URL` 的用户段。

不带驱动类时，看 `VERDICT: FAIL` 之后的引导词（自上而下、第一条命中即停）：

- 类名冒号后是 `<error text withheld: redaction unavailable (<异常类>)>`，**不论什么类** → 原文被扣下，
  是因为车道导不进自己的脱敏模块 `packages.common.redaction`，它在模块层 import `psycopg2.extensions`。
  最常见的原因是 venv 里的驱动 `psycopg2` 坏了或没了：展示模块不依赖驱动，配置期那次导入照常通过，
  所以缺驱动落在**退 3**、不在 §11.5 的退 2；`create_engine` 去 import `psycopg2` 失败，同一个缺包
  又让脱敏失败。**不是** DSN 的事：别去改 env 文件，也别拿下面那条的探针分——探针同样会以缺驱动
  失败，被误读成"`DATABASE_URL` 解析不了"。→ §11.3「非驱动观测失败」开头的驱动检查。2026-09-18
  node-27 实测（生产 venv、生产 env 文件，用一个 scratch `sitecustomize` 挡掉 `psycopg2`）：车道读
  `ModuleNotFoundError: <error text withheld: redaction unavailable (ModuleNotFoundError)>`、退 3，
  单测钉住同一形状。
- `observation failed:` 且类是 `ValueError` / `ArgumentError`（URL scheme 不认识时是其子类
  `NoSuchModuleError`）→ **先做 §11.3「库侧观测失败」第 2 步**（先查 unit 读哪份 env 文件，再跑
  探针），别凭异常类直接去改 env 文件：观测期的展示模块同样可能抛 `ValueError`。探针以**同一个**异常失败 → env 文件里的
  `DATABASE_URL` 解析不了，改 §11.4 那份 env 文件；探针成功 → 异常来自观测期，走 §11.3
  「非驱动观测失败」。`ValueError` 这条实测过：DSN 端口写成 `notaport` 时车道报
  `ValueError: invalid literal for int() with base 10: 'notaport'`，探针原样复现同一行、`rc=1`；
  `ArgumentError` / `NoSuchModuleError` 是按 SQLAlchemy 解析 URL 的行为推导的。
- `observation failed:` 且是其它类 → 观测期展示模块（`national_discharge_cycles`）抛的 → §11.3
  「非驱动观测失败」。推导。
- `observation unusable:` → 观测拿到了，但 `evaluate()` 拒收（例如 `default_cycle` 解析不了）→
  §11.3「非驱动观测失败」。推导。

展示模块报错分**导入期**与**观测期**两种，落在哪个退出码取决于它发生在哪一期：`config_from_env` 先读
`DATABASE_URL`、再调 `lookback_days()` 去 import `services.tiles.mvt`，所以**导入期**
的失败在任何数据库动作之前就发生，按配置错误退 2；**观测期**（已连上库、正在调
`national_discharge_cycles`）才是退 3。两者第一步不同：退 2 → §11.5「导入期失败」（导入检查的最后一行分出
仓库代码缺陷 / 缺第三方依赖 / 已装的第三方包与代码对不上 / 检查通过则手跑 unit 复核）；退 3 → 照上面的「退 3 路由」读 `reason`，与别的退 3 没有区别——观测期展示模块自己发的
SQL 失败了，`reason` 就带 `(psycopg2.`，照样去 §11.3「库侧观测失败」；不带的才去「非驱动观测失败」。

邮件正文就是 `journalctl -n 30` 的尾巴。报告刻意**表在前、`VERDICT:` 块在最后**，
且总行数 ≤ 24 —— verdict 必须活在尾窗里。尾窗预算要**分两类算，不能合成一个数**：
退 1（告警）那条路径上 **systemd 自己占 5 行框架**（`Starting…`、`Main process
exited…`、`Failed with result…`、`Failed to start…`、`Triggering OnFailure=
dependencies.`），而**本车道在该路径一行结构化 stderr 都不打**——告警路径调 `_emit`
时不带 `structured=`。`24 + 5 = 29 ≤ 30`，只剩 1 行余量。结构化 stderr 行只出现在
退 2 / 退 3，而那两条路径的报告只有两行 `VERDICT:`，尾窗绰绰有余。

source 太多时表会被截断并打一行 `… N more sources omitted`，**排序保证超阈的 source
先打印**——但这条保证是**有界的、不是绝对的**：失败时 `VERDICT:` 块占 3 行，留给表的
额度是 `24 − 1 − 3 = 20` 行，一旦 source 数超过 20 就压成 19 行表 + 1 行省略说明——
截断一发生表就只剩 19 行，所以超阈 source 多于 19 个时**超阈的行也会被截掉**。
截掉不等于隐瞒：表头的
`breaching=<n>` 是真数、省略行带精确的丢弃条数、`VERDICT: breaching=` 仍点名前 8 个
source 并补 `+N more`。把表撑大反而会把 verdict 顶出尾窗，那是更坏的失败（设计 D3）。
DSN 口令在任何面（stdout、stderr、异常文本）都被脱敏。

本车道**无状态**：没有 state 文件、没有 lock、没有 receipt、没有自己的日志文件。因此
也**没有 dedup**——故障不消除就每天一封。沉默来自消除故障，不是压制告警。

### 11.3 处置：退 1 的三个真分支 + 退 3 的三条

收到 `VERDICT: FAIL ... behind ingest ...` 时，先用 `VERDICT: breaching=` 那行拿到
出问题的 source，再分支：

**分支 A —— coverage 刷新停摆（主因）**

```bash
ssh -p 32099 nwm@210.77.77.27
grep -n 'coverage_refresh\|coverage backstop' /home/nwm/autopipe-logs/*.log | tail -40
```

看到 `refresh_failed_rc<N>` 或 backstop 非零 rc，就是刷新腿在失败（`rc=3` 是 #1446 的
拒绝守卫）。修完刷新后手工补跑 `scripts/node27_refresh_coverage.py`，下一 tick 自动闭环。补跑的命令、
必须带的写角色 env 与 `rc=3` 时 `--force` 的纪律，都在下面分支 C 末尾的刷新块里；其它非零 rc 在本
runbook 没有专门的处置段，把该行前后的日志留证开 issue（推导）。想立刻确认，就在补跑之后用 §11.4
那条 `systemctl --user start` 手跑一次（读法见 §11.4 末尾）。日志里没有失败行 → 走分支 B。
注意：刷新**故意保持 non-fatal**（设计 D7，ingest 成功不该因刷新失败变成失败），所以
rc 只在日志里，本车道才是那个持久信号。

**分支 B —— 某个 river network 不产出 / 新激活但没有可展示 run**

日志里刷新腿一切正常时，问题在**网络集合**而不在刷新脚本：全国目录是**交集、fail-closed**，
只要有一个 `active_flag` 的 network 对某 cycle 没有可展示 run，该 cycle 整条被关掉。
新激活一个网络（`_national_discharge_coverage_rows` 会把每个 cycle 都关掉）同样会推大 gap。
这是**真阳性**，刷新脚本救不了——去查是哪个 network：

```sql
-- 活跃网络集合（分母）
SELECT DISTINCT mi.river_network_version_id
FROM core.model_instance mi
WHERE mi.active_flag AND mi.river_network_version_id IS NOT NULL
ORDER BY 1;
-- 该 source 最新 cycle 上实际有可展示 run 的网络（分子）
SELECT DISTINCT mi.river_network_version_id
FROM hydro.hydro_run h
JOIN core.model_instance mi ON mi.basin_version_id = h.basin_version_id
WHERE lower(h.source_id) = 'gfs'
  AND h.status IN ('succeeded','parsed','published')
  AND h.cycle_time = (SELECT max(cycle_time) FROM hydro.hydro_run
                      WHERE lower(source_id) = 'gfs'
                        AND status IN ('succeeded','parsed','published'))
ORDER BY 1;
```

差集里的网络就是压住全国图层的那个：要么让它重新产出，要么按业务裁定把它
`active_flag` 置 false（退出业务化的口径见 §7）。置 false 不需要补刷新：目录的活跃网络集合是
`services/tiles/mvt.py` 的 `_national_discharge_coverage_rows` 每次现读 `core.model_instance`（`mi.active_flag`）
得来的，没有物化；重新产出的 run 由 autopipe 自己的 coverage 刷新腿写覆盖行，那条腿失败是分支 A 的事。
处置之后直接用 §11.4 那条 `systemctl --user start` 手跑一次（读法见 §11.4 末尾）。仍退 1 → 回来重跑上面
两条语句，差集已空就走分支 C（推导）。差集为空 → 直接走分支 C。

**分支 C —— A 和 B 都查空：目录仍然不收这个 cycle**

刷新腿日志干净（分支 A 没线索）、活跃网络集合与分子集合相等（分支 B 差集为空），
gap 却还在涨。**分支 B 差集为空并不证明覆盖行在**：它的分子语句根本不 JOIN
`hydro.run_display_coverage`，所以一个"有可展示 run、却没有覆盖行"的网络在 B 里照样
是空差集——那正是 §11.1 点名的 `no_coverage_row` rc=0 静默路径
（`scripts/node27_autopipeline.py:2207-2209`）。因此走到这里有两种落点：要么覆盖行缺失
或为零，要么覆盖行确实在、而 `services/tiles/mvt.py` 的**覆盖窗矩形校验**
（`_national_coverage_window`）或 **3h 相位校验**（`_national_cycle_valid_times`）把该
cycle 丢掉了。后两处都是 fail-closed 的：任一网络的行不过关，整条 cycle 就不进列表。
逐条对这个 source 最新几个 cycle 查四项判据（下面用 LEFT JOIN，就是为了让第一种落点现身）：

```sql
-- 与 `_national_discharge_coverage_rows` 同构：每个 (network, cycle) 只判最新的那个
-- run（`rn = 1`），否则重导 cycle 上的陈旧 run 会把你指向目录根本不看的那一行。
WITH ranked AS (
  SELECT h.run_id, h.cycle_time, mi.river_network_version_id,
         ROW_NUMBER() OVER (
           PARTITION BY mi.river_network_version_id, h.cycle_time
           ORDER BY h.run_id DESC
         ) AS rn
  FROM hydro.hydro_run h
  JOIN core.model_instance mi ON mi.basin_version_id = h.basin_version_id
  WHERE lower(h.source_id) = 'gfs'
    AND h.status IN ('succeeded','parsed','published')
    AND mi.active_flag AND mi.river_network_version_id IS NOT NULL
)
SELECT r.run_id, r.cycle_time, r.river_network_version_id,
       rdc.segment_count, rdc.river_sample_count,
       rdc.min_lead_time_hours, rdc.max_lead_time_hours,
       rdc.river_valid_time_start, rdc.river_valid_time_end,
       -- 矩形校验：窗口列齐 + 样本数等于 段数 × lead 数 + 跨度等于 (lead 数 - 1) h
       (rdc.river_valid_time_start IS NOT NULL
        AND rdc.river_valid_time_end IS NOT NULL
        AND rdc.min_lead_time_hours IS NOT NULL
        AND rdc.max_lead_time_hours IS NOT NULL) AS window_cols_ok,
       (rdc.river_sample_count
        = rdc.segment_count * (rdc.max_lead_time_hours - rdc.min_lead_time_hours + 1)) AS sample_count_ok,
       (EXTRACT(EPOCH FROM (rdc.river_valid_time_end - rdc.river_valid_time_start))::bigint
        = (rdc.max_lead_time_hours - rdc.min_lead_time_hours) * 3600) AS span_ok,
       -- 相位校验：窗口起点必须落在该 cycle 的整点网格上
       (EXTRACT(EPOCH FROM (rdc.river_valid_time_start - r.cycle_time))::bigint % 3600 = 0) AS phase_ok
FROM ranked r
-- LEFT JOIN，不是 INNER：缺覆盖行的 run 必须以 segment_count 等 rdc.* 全 NULL 现身，而不是从结果里消失。
LEFT JOIN hydro.run_display_coverage rdc ON rdc.run_id = r.run_id
WHERE r.rn = 1
ORDER BY r.cycle_time DESC, r.run_id DESC
LIMIT 20;
```

四个布尔列里出现 `false` 或 `NULL` 的那一行，就是压住 cycle 的 run。两种读法靠
`rdc.segment_count` 分：**它为 NULL** 就是这个 run 压根没有覆盖行，即上面那条
`no_coverage_row`，处置是直接补刷新，不必查几何；`segment_count` 有值而布尔列挂，才是
几何问题。缺行的那一行**不会"整行 NULL"**：`window_cols_ok` 是一串 `IS NOT NULL` 的合取，
按三值逻辑永不为 NULL，缺行时它读 `false`（psql 显示 `f`），而 `sample_count_ok`、
`span_ok`、`phase_ok` 连同所有 `rdc.*` 值列才是 NULL（psql 里留空）——认这个形状即可。
还要留一手：目录侧的 JOIN 多带
`rdc.segment_count > 0`，最新 run 缺行或被归零时它会**回退到该 cycle 上次新的 run**，
所以最新行为 NULL/零时要回头看同 cycle 更早的 `run_id` 是不是正扛着覆盖。**这一类不是假想
的**：覆盖行由 `packages/common/display_coverage.py` 一次扫描算出，`river_sample_count`
是**所有**样本之和，而 `min/max_lead_time_hours` 取的是各河段 lead 区间的**交集**
（`MAX(min)` / `MIN(max)`），河段之间 lead 覆盖参差时这两个数就对不上；
`river_valid_time_start/end` 只统计"该时刻河段数 = 期望河段数"的完整时刻，写入残缺时
整段是 NULL；输出时间网格不落在 cycle 的整点上则相位校验挂。三种几何都保持
`segment_count > 0`，所以分支 A、B 必然查得干干净净——这正是这一类会把人卡住的原因。

**四个布尔列不是完整的拒绝集合**：它们只覆盖单行的矩形与相位，而
`_national_cycle_valid_times` 还做**跨行**判定——各网络窗口取交集后为空
（`window_end < window_start`），或交集被 cycle 截断后**一个 3h 步长时刻都装不下**
（`last_index < first_index`）。后者尤其反直觉：`min_lead = max_lead = 1` 时四个布尔全
`true`（跨度 0 = `(lead 数 - 1) × 3600`，相位整除），该 cycle 照样被丢。所以四列全 `true`
还查不出原因时，别再看单行：对该 cycle 取活跃网络上的 `max(river_valid_time_start)` 与
`min(river_valid_time_end)`，看 `cycle_time + 3k h` 有没有落进这个区间——装不下就是
lead 跨度太短或窗口交集为空，同样归上游产出。

处置：对这些 run 重跑覆盖刷新，让窗口列按当前数据重算。**这是一次写操作**
（`packages/common/display_coverage.py` 的 `_REFRESH_SQL` 是 `INSERT … ON CONFLICT DO UPDATE`），
所以必须带写角色的 DSN：源 `infra/env/node27-ingest.env`（`nhms_ingest_rw`），**不要**源
§10/§11.4 那份告警 env —— 它带的是只读的 `nhms_display_ro`（见上面的角色表），
会在 `InsufficientPrivilege` 上抛栈退 1，既不是刷新成功的 rc=0，也不是 #1446 拒绝守卫的 rc=3。
生产刷新腿走的就是这份 ingest env（`scripts/node27_autopipe_cron.sh` 源它之后调同一个脚本）。

```bash
cd /home/nwm/NWM
set -a; . infra/env/node27-ingest.env; set +a
PYTHONPATH=/home/nwm/NWM .venv/bin/python scripts/node27_refresh_coverage.py --run-id <run_id>
echo "rc=$?"
```

新扫描算出非空时会直接覆盖旧的窗口列，**不需要** `--force`。只有新扫描算成空时
（#1446 拒绝守卫退 **3** 并打一行 `DISPLAY_COVERAGE_REFRESH_REFUSED`，见 §3.1）才谈得上
`--force`，而且必须运维逐条确认后再加：`--force` 的动作是把行**归零**，对一个正被全国
图层使用的 run 用它就是直接熄灯，别拿它当默认手段。

刷新完把上面那条四判据 SQL 再跑一遍（四列全 `true` 的还要按前面「四个布尔列不是完整的
拒绝集合」那段的 `max(river_valid_time_start)` / `min(river_valid_time_end)` 跨行判定再看
一眼），全绿之后**再验一次车道本身**，别等下一 tick：用 §11.4 那条
`systemctl --user start nhms-node27-coverage-freshness-alert.service` 看退出码（读法见 §11.4 末尾：
`systemctl` 返回 0 即车道退 0；非零时车道的退出码在 journal 的 `status=<N>` 里，而且会照发一封邮件）。
**要走 unit**——它自带 `EnvironmentFile=`，跑出来的是本车道真正用的只读
`nhms_display_ro` DSN；若只从 shell 历史里重跑 §11.5 那段手工调用的 python 那一行、没有
重新源 `infra/env/node27-frontier-alert.env`，环境里还留着刚才刷新用的
`infra/env/node27-ingest.env`，那是以写角色 `nhms_ingest_rw` 在跑，压根没检验只读角色的
grant，可能给你一个假绿（要跑就把那两行整段一起跑）。退 0 即闭环：本车道无状态、无
dedup、也不发"恢复"邮件（口径见 §11.2 末尾那两行），此后下一个预定信号就是下一个 06:00
那一 tick。

重算后判据仍不过关的，问题在上游产出（河段样本残缺 / 输出网格相位），刷新脚本修不了：
本 runbook 没有对应的处置段，把 `run_id`、四个布尔列与跨行判定的实测值、以及
`/home/nwm/autopipe-logs/*.log`（分支 A 用的同一批日志）里该 `run_id` 的 ingest / parse
行留成证据，对 parse/output 侧开 issue。

**退出 3 —— 库侧观测失败（`COVERAGE_FRESHNESS_OBSERVATION_FAILED`，`reason` 带 `(psycopg2.`）**

先诊断、别急着重建：下面四步的诊断动作都只读，写操作只出现在查明原因之后的处置里。重建
`nhms-db` 容器是再往后一跳的事（§5.1 有指针），不是收到这封信的第一步。按 §11.2「退 3 路由」查到是哪一步就从哪一步做起。

**第 1 步 —— 容器在不在、端口映射对不对**

```bash
ssh -p 32099 nwm@210.77.77.27
docker ps -a --filter name=^nhms-db$ --format '{{.Names}} {{.Status}} {{.Ports}}'
```

正常读 `nhms-db Up <时长> … 127.0.0.1:55432->5432/tcp`。状态不是 `Up`、或没有
`127.0.0.1:55432->5432/tcp` → 是库本身的事故，不是本车道的，按 §5.1（容器事实与重建指针）
处理。告警是 `AdminShutdown` → 不论 `Up` 的时长：时长很短是观测中途容器重启过，时长很长是车道的
后端连接被人终止了（`pg_terminate_backend`，口径见 §9.2）。本车道无状态，库恢复后下一 tick 自然转绿；
要立刻确认就用 §11.4 那条 `systemctl --user start` 手跑一次，按 §11.4 末尾的读法看退出码（推导）。
健康（`Up`、映射是 `127.0.0.1:55432->5432/tcp`）而告警不是 `AdminShutdown` → 库本身在听，问题在车道
这一侧：做第 2 步。若你是从第 2 步探针读法跳回来的，就回到那一条接着做它的下一个动作，别再从第 2 步开头来
（推导）。

**第 2 步 —— 先看 unit 读哪份 env 文件，再跑探针**

第一个动作不碰库，只问 systemd。探针（以及 §11.5 那段手工调用）源的是仓库里的
`infra/env/node27-frontier-alert.env`，而 unit 读的是它自己 `EnvironmentFile=` 列表里的文件——
§11.5 验投递时没删干净的 scratch drop-in 会把这个列表换掉。两边不一致时探针验的是另一个 DSN，
它的读数全都不算数，所以这一步必须在探针之前：

```bash
systemctl --user show -p EnvironmentFiles nhms-node27-coverage-freshness-alert.service
```

健康时**恰好**读这一行（2026-09-18 node-27 实测）：

```text
EnvironmentFiles=/home/nwm/NWM/infra/env/node27-frontier-alert.env (ignore_errors=no)
```

读到别的（别的路径、多出一行、或空）→ 原因就在这里，不在库。问 systemd 是哪些 drop-in 在起作用：

```bash
systemctl --user show -p DropInPaths nhms-node27-coverage-freshness-alert.service
```

它列出**所有** drop-in：持久的（`~/.config/systemd/user/nhms-node27-coverage-freshness-alert.service.d/`）
与运行时的（`systemctl --user edit --runtime` 写进 `/run/user/<uid>/systemd/user/…service.d/` 的）都在内。
只 `ls` 持久目录会漏掉后者，而重装 unit 文件也清不掉它们。健康时读空的 `DropInPaths=`、`rc=0`
（2026-09-18 node-27 实测）。把列出的、
路径里带 `nhms-node27-coverage-freshness-alert.service.d/` 的文件逐个 `rm`，`systemctl --user daemon-reload`，
再 show 一次 `DropInPaths` 与 `EnvironmentFiles`，确认回到上面那一行，然后用 §11.4 那条
`systemctl --user start` 手跑一次，按 §11.4 末尾的读法看退出码。`DropInPaths` 里没有本 unit 的 drop-in、`EnvironmentFiles`
却仍不是上面那一行 → 装进去的 unit 文件被改过，重跑 §11.5 安装块里的两条 `install` 与
`daemon-reload`，再用 §11.4 那条 `systemctl --user start` 手跑一次确认。（`EnvironmentFiles` 与
`DropInPaths` 的健康值是实测；异常读数这两支是推导。）

读数正确之后跑**探针**：走本车道**自己的** venv、env 文件与只读角色，用与车道同一个 SQLAlchemy
URL 解析，并报出连到了哪个库、本车道的表在不在：

```bash
cd /home/nwm/NWM
set -a; . infra/env/node27-frontier-alert.env; set +a
PYTHONPATH=/home/nwm/NWM .venv/bin/python -c '
import os, sqlalchemy
engine = sqlalchemy.create_engine(os.environ["DATABASE_URL"], connect_args={"connect_timeout": 10})
with engine.connect() as conn:
    print(conn.execute(sqlalchemy.text("select current_database(), current_user, to_regclass(:rel) is not null"), {"rel": "hydro.hydro_run"}).one())
'; echo "rc=$?"
```

健康读 `('nhms', 'nhms_display_ro', True)` 加 `rc=0`（2026-09-18 node-27 实测）。其余读法——凡是
「改 env 文件」的，改的都是 §11.4 那份 env 文件里的 `DATABASE_URL`，改完用 §11.4 那条
`systemctl --user start` 手跑一次，按 §11.4 末尾的读法确认 `systemctl` 返回 0：

- 库名不是 `nhms`、或第三列是 `False` → DSN 指错了库。unit 读的就是这份文件（上面第一个动作已确认），
  改 env 文件。
- 探针自己也以基类 `psycopg2.OperationalError` 失败 → 按 §11.2 的原文分：
  - `Connection refused` → 先看第 1 步的容器读数。容器不是 `Up`、或没有 `127.0.0.1:55432->5432/tcp`
    → 第 1 步的 §5.1 那一条。容器 `Up`、映射也对，探针却照样被拒 → DSN 的 host:port 与映射对不上：
    原文里 `connection to server at "<host>", port <N>` 的 `N` 不是 `55432`（或 host 不是 `127.0.0.1`
    / `localhost`）→ 改 env 文件。形状实测：2026-09-18 在 node-27 上，容器健康、DSN 端口写成 `1` 时车道读
    `OperationalError: (psycopg2.OperationalError) connection to server at "127.0.0.1", port 1 failed: Connection refused`；
    这条路由是推导。
  - `password authentication failed` / `database "…" does not exist` → 改 env 文件（两种原文实测）。
  - 常见三种之外的原文（`timeout expired`、`too many clients` 一类）→ 第 1 步看容器、再第 3 步看
    `pg_stat_activity`（推导）。
- 探针以 `ProgrammingError: (psycopg2.errors.InsufficientPrivilege)` 失败（`permission denied for schema …`）→
  连库的角色不是 `nhms_display_ro`（第 4 步核对的就是它），读 env 文件 `DATABASE_URL` 的用户段，改 env 文件
  （原文形状 2026-09-18 node-27 以 `nhms_download_rw` 身份实测；路由推导）。
- 探针以 `ValueError` / `ArgumentError` / `NoSuchModuleError` 失败、且与告警是同一行 → DSN 解析不了，
  改 env 文件（`notaport` 那一例实测，见 §11.2「不带驱动类」第二条）。
- 探针一切正常，按告警分：
  - 告警是 `Undefined*`（`UndefinedTable` / `UndefinedColumn` / `UndefinedFunction`…）→ 下一条。
  - 告警是 `ValueError` / `ArgumentError` / `NoSuchModuleError` → 异常来自观测期，走下面
    「非驱动观测失败」（§11.2「不带驱动类」第二条）。
  - 从下面「什么都观测不到」第 1 步过来的 → 库连对了，回那一条做第 2 步。
  - 从第 4 步末尾那段过来的（告警是 `InsufficientPrivilege`）→ 回那一段，按 env 文件 `DATABASE_URL` 的用户段分。
  - 告警是别的驱动错误（拒连、口令、`AdminShutdown`、`timeout expired`…）→ 是已经过去的瞬时故障：
    手跑一次 unit，`systemctl` 返回 0（即车道退 0，读法见 §11.4 末尾）即闭环。仍失败、原文不变 →
    unit 与探针用的不是同一套连接参数，而 `EnvironmentFiles` 已确认一致：把两边原文与 `DropInPaths` 读数
    一并开 issue（推导）。
- 探针一切正常、而告警是 `Undefined*`（`UndefinedTable` / `UndefinedColumn` / `UndefinedFunction`…）
  → 库连对了，是**部署的代码与库的 schema 对不上**（schema 漂移）。探针只查 `hydro.hydro_run`
  在不在，而车道还要读 `hydro.run_display_coverage` 与 `core.model_instance` 的若干列、以及
  `services/tiles/mvt.py` 目录语句用到的对象，所以探针健康证明不了它们都在。下面这一支整条是推导。

探针的报错只落在你自己的终端，但驱动报错可能回显 DSN，别原样贴进 issue。

**schema 漂移那一支**：先读 `reason` 里点名的对象（`relation "…"`、`column … does not exist`、
`function …`），再看代码和迁移账本两头：

```bash
cd /home/nwm/NWM
git log -5 --oneline -- services/tiles/mvt.py scripts/node27_coverage_freshness_alert.py
comm -3 <(ls db/migrations | grep '\.sql$' | sort) \
        <(docker exec nhms-db psql -X -U nhms -d nhms -Atc "select version from public.schema_migrations" | sort)
docker exec nhms-db psql -X -U nhms -d nhms -P pager=off -c '\d <schema.table>'
```

`comm -3` 顶格的行是这棵树里有、账本 `public.schema_migrations` 里没有的迁移（代码期待、库没施加）；
缩进一列的是账本里有、这棵树里没有的。**健康的 node-27 并不是空输出**（2026-09-18 实测）：顶格 0 行、
缩进 7 行（`000007_flood.sql`、`000015_…`、`000017_…`、`000020_…`、
`000031_search_discovery_return_period_performance.sql`、`000034_…`、`000036_…`），都出自
`b97c16e28`「Remove retired frequency display pipeline」：其中 6 个是被从树里删掉的已退役频率展示管线
迁移，`000031` 则是被**改名**成了 `000031_search_discovery_performance.sql`——账本仍记着旧名，所以
旧名缩进；新名在树里、也在账本里，所以不顶格。缩进行因此本身不是信号；这一支的信号是**顶格行**（代码期待、账本
没有），即便有顶格行也仍以下面的 `\d` 为准。账本记的是 `packages/common/migrate.py` 施加过的
迁移文件名，而 `psql -f` 直接跑的迁移**不写**账本（`tier-node27-timeseries-storage.md` 里「施加
000052 的运维口径」那段；拿账本对照 `db/migrations` 的既有口径见同一份 runbook §9.6），所以
账本只是旁证；**对象在不在以 `\d` 为准**——把 `reason` 点名的表代进 `<schema.table>`，缺列就看列清单里有没有它；缺函数改用 `\df <函数名>`。

处置**不是**在生产库上手工补迁移：迁移由超级用户 `nhms` 经 `packages/common/migrate.py` 施加，
施加后还要重跑 `scripts/node27_provision_write_roles.sh`（§5.1），那是一次需要另行批准的变更。
收到这封的动作是开 code / migration issue，附 stderr 那行结构化 JSON 里完整的 `reason`（`VERDICT:` 行是
拍平、截断过的）、上面 `git log` 与 `comm -3` 的输出、以及 `\d` 的结果。本车道无状态、无 dedup，
修好之前每天 06:00 一封。

**第 3 步 —— statement 超时（`QueryCanceled`）**：车道每条语句限 30 s，看是谁在跟它抢：

```bash
docker exec nhms-db psql -X -U nhms -d nhms -P pager=off -c "
select pid, usename, application_name, state, wait_event_type,
       now() - query_start as dur, left(query, 80)
from pg_stat_activity
where datname = 'nhms'
order by query_start;"
```

用超级用户 `nhms` 走 `docker exec`，是因为只读角色看不到别的角色会话的语句文本。这条语句自己也卡住或
连不上（容器挂死时就是这样）→ 不是争用，是库本身的事故，按 §5.1 处理（推导）。
2026-09-18 实测的一次常态：一行 `nhms_ingest_rw`、`active`、`IO` 的
`SELECT compress_chunk(...)`（压缩 timer 的正常后台工作）。本车道自己的连接读空串
`application_name`、`usename` 是 `nhms_display_ro`（代码里没设名字，见 §9.2 表里「空串」那行）。
归因与取消纪律一律按 §9.2——生产 tick 不得随手取消；争用过去后手跑一次 unit 确认（读法见 §11.4 末尾）。天天超时属于
容量 / 计划问题，按 §9.2 走 issue。

**第 4 步 —— 权限拒绝（`InsufficientPrivilege`）**：`nhms_display_ro` 需要 `hydro`、`core` 两个
schema 的 USAGE，以及 `hydro.hydro_run`、`hydro.run_display_coverage`、`core.model_instance` 三张表的
SELECT。一条语句全核对：

```bash
docker exec nhms-db psql -X -U nhms -d nhms -P pager=off -Atc "
select 'schema ' || s, has_schema_privilege('nhms_display_ro', s, 'USAGE')
from unnest(array['hydro', 'core']) as s
union all
select 'table ' || rel, has_table_privilege('nhms_display_ro', rel, 'SELECT')
from unnest(array['hydro.hydro_run', 'hydro.run_display_coverage', 'core.model_instance']) as rel;"
```

健康时五行全 `t`（2026-09-18 node-27 实测）。读 `f` 的那行就是缺的：

- `schema hydro|f` / `schema core|f` → 缺 schema 的 USAGE，驱动原文是
  `permission denied for schema …`。**这时三张表照样读 `t`**——`has_table_privilege` 只看表自己的
  ACL，只查表会查不出来（推导）。`hydro`、`core` 两个 schema 的 owner 都是 `nhms`（2026-09-18
  node-27 实测），所以 USAGE 由超级用户 `nhms` 授予。
- `table …|f` → 缺那张表的 SELECT，驱动原文是 `permission denied for table …`。由关系 owner
  `nhms_ingest_rw` 或超级用户 `nhms` 补（角色见 §5.1 角色表）。

补 grant 是一次权限变更。先想清楚它是怎么丢的：display API 也以
`nhms_display_ro` 读这三张表（`national_discharge_cycles` 就是它发布的目录），它若同时在报错，
这是一次波及展示面的权限回退，不只是本车道的事。补完用 §11.4 那条 `systemctl --user start` 手跑一次，
按 §11.4 末尾的读法确认 `systemctl` 返回 0。

五行全 `t`、告警却是 `InsufficientPrivilege` → 被拒的不是这五样，或连库的不是 `nhms_display_ro`
（推导）。读驱动原文里 `permission denied for <对象>` 点名的是什么，再看连库的是哪个角色：先做第 2 步的第一个
动作确认 unit 读的是哪份 env 文件，然后读那份文件里 `DATABASE_URL` 的用户段（邮件里角色名被脱敏成 `***`，见
§11.2）——不要靠探针的 `current_user`：角色若没有 `hydro` 的 USAGE，探针自己就以
`permission denied for schema hydro` 失败、打不出 `current_user`（2026-09-18 node-27 实测：以
`nhms_download_rw` 身份跑探针里的 `to_regclass('hydro.hydro_run')` 即如此）。用户段不是 `nhms_display_ro` →
DSN 用错了角色，改 §11.4 那份 env 文件；是 → 被拒的对象是车道
或 `services/tiles/mvt.py` 新引用、而只读角色没被授权的，按上面 schema 漂移那一支同样的材料开 issue
（附完整 `reason`），别在生产库上临时补授。

**退出 3 —— 非驱动观测失败（`COVERAGE_FRESHNESS_OBSERVATION_FAILED`，`reason` 不带 `(psycopg2.`）**

`reason` 里是 `<error text withheld: redaction unavailable (…)>` 的，先查驱动与脱敏模块导不导得进，
别往下走：

```bash
cd /home/nwm/NWM
PYTHONPATH=/home/nwm/NWM .venv/bin/python -c 'import psycopg2; import packages.common.redaction; print(psycopg2.__version__)'; echo "rc=$?"
```

健康读 `2.9.12 (dt dec pq3 ext lo64)` 加 `rc=0`（整条命令 2026-09-18 node-27 实测）。失败时按它打印的最后一行套 §11.5「导入期失败」的三条失败分支：`No module named 'psycopg2'`
这类是「缺第三方依赖」那支（发行包名是 `psycopg2-binary`；sync 之后重跑的是**这条**驱动检查，不是
§11.5 那条 `import services.tiles.mvt`——它不碰驱动，缺驱动时照样通过），`packages.…` 出错是「仓库代码缺陷」那支；
驱动装着却导不进（例如 `ImportError: libpq.so.5: cannot open shared object file …`，2026-09-18 node-27
模拟实测：车道退 3、`reason` 读 `ImportError: <error text withheld: redaction unavailable (ImportError)>`）
是「已装的第三方包与代码对不上」那支，拿 `psycopg2-binary` 对版本。两者都导得进（`rc=0`）→ 先用
§11.4 那条 `systemctl --user start` 手跑一次：`systemctl` 返回 0（读法见 §11.4 末尾）→ 是已经过去的瞬时
故障（例如 tick 撞上了一次正在进行的 sync），闭环；仍失败、`reason` 仍被扣 → 脱敏函数自己在这个异常上
出了错，按下面第 3 条当代码缺陷开 issue（推导）。

`ValueError` / `ArgumentError` / `NoSuchModuleError` 先按 §11.2「退 3 路由」做上一条第 2 步（先查
unit 读哪份 env 文件，再跑探针）排除 DSN；探针成功的、别的异常类的、以及引导词是
`observation unusable:` 的，才到这里。
这是展示模块（`services/tiles/mvt.py`）或本车道与它之间契约的**代码缺陷**，不是运维旋钮：

1. 用 §11.5 那段手工调用复现。它源的是仓库里那份 env 文件，unit 读的也是它时就会回来同一行。
   复现不出 → 回到上面「库侧观测失败」第 2 步的第一个动作查 `EnvironmentFiles`：unit 读的可能
   根本不是这份文件（推导）。`EnvironmentFiles` 也健康 → 用 §11.4 那条 `systemctl --user start` 手跑一次：
   `systemctl` 返回 0（读法见 §11.4 末尾）→ 瞬时，闭环；仍失败 → 带上 unit 这次的 `reason` 与手工调用的输出，
   直接做第 3 条（推导）。
2. 看展示模块最近动过什么：`cd /home/nwm/NWM && git log -5 --oneline -- services/tiles/mvt.py`。
3. 按代码缺陷开 issue，附上 stderr 那行结构化 JSON 里完整的 `reason`（`VERDICT:` 行是拍平、
   截断过的）。本车道无状态、无 dedup，修好之前每天 06:00 一封。

**退出 3 —— "什么都观测不到"（`COVERAGE_FRESHNESS_NO_SOURCES`，fail-closed，不是健康）**

本条只管 `COVERAGE_FRESHNESS_NO_SOURCES`。另一个退 3 的 code
`COVERAGE_FRESHNESS_OBSERVATION_FAILED` 的所有原因按 §11.2「退 3 路由」去上面两条（库侧 /
非驱动）。

ready 前沿查询返回**零个 source key** 时本车道**退 3**，不是退 0。那条语句 JOIN 了
`core.model_instance`，所以一次批量 `active_flag` 翻转或 `river_network_version_id`
漂移就能把它清空，而 `hydro.hydro_run` 照常前进——§10 的车道不 JOIN 那张表，看得见
"进展"因而继续沉默，同时 `national_discharge_cycles` 已经在返回 `default_cycle = null`，
图层已经黑了。这正是本 issue 要消除的构造性沉默。收到这封：

1. 先确认车道读的是哪个库：做上面「库侧观测失败」第 2 步——先查 `EnvironmentFiles`（§11.5 验投递时
   留下的 scratch drop-in 会把 unit 指到一个有表、没数据的库，在那里查就是零行），再跑探针看
   `current_database()`。库不对 → 那一步的读法已经给出处置（改 env 文件或删 drop-in，再手跑确认）。
2. 库对了，再查 `core.model_instance` 里还剩多少能进 ready 前沿的活跃实例：

   ```bash
   docker exec nhms-db psql -X -U nhms -d nhms -Atc "select count(*) filter (where active_flag), count(*) filter (where active_flag and river_network_version_id is not null) from core.model_instance;"; echo "rc=$?"
   ```

   输出是 `<活跃实例数>|<其中带 river network 的数>`，健康时读 `38|38`、`rc=0`（2026-09-18 node-27 实测；列与
   谓词取自车道 ready 前沿语句的 `mi.active_flag` 与 `mi.river_network_version_id IS NOT NULL`）。读法（推导）：
   - 第二个数是 `0` → 就是上面说的批量 `active_flag` 翻转或 `river_network_version_id` 漂移。恢复
     哪些实例是业务裁定（口径见 §7），不是本车道能定的；恢复之后用 §11.4 那条 `systemctl --user start`
     手跑一次，按 §11.4 末尾的读法确认。
   - 第二个数大于 `0` → 实例在，但它们的 `basin_version_id` 上没有任何 `succeeded` / `parsed` /
     `published` 且带 `cycle_time` 的 run：实例指向了一个还没产出过的 basin version，或 ingest 从没为它
     产出。这是 ingest 侧的事——看 §10 前沿车道与 §3.1 的 ingest 口径；把这两个计数与探针读数一并开 issue。

缺 grant **不会**走到这里：权限不足抛 `InsufficientPrivilege`，是
`COVERAGE_FRESHNESS_OBSERVATION_FAILED`，不会静默返回零行（`db/` 下没有行级安全策略）。
**注意区分**：有 source key 但全部老出回看窗，是另一个状态，退 0 并全记 `not-evaluated`，
归 §10 管。

### 11.4 阈值旋钮 `NHMS_COVERAGE_GAP_DAYS`（改之前先读这段）

写进 `infra/env/node27-frontier-alert.env`（与 §10 同一份文件），单位是**天**，浮点。

- 合法区间：**有限数、严格大于 0、严格小于 `NATIONAL_DISCHARGE_CYCLE_LOOKBACK_DAYS`**
  （当前 12）。`abc` / `0` / `-1` / `inf` / `nan` / `>= 12` 一律**退 2**，绝不静默夹取——
  阈值取到窗口或以上，只能在图层**已经黑了**之后才触发，那就不是探测器了。
- 不设该变量时用 `回看窗 / 3`。**不要在这里写死天数**：窗口常量改小时，默认阈值自动跟着小。
- 改完直接 `systemctl --user start nhms-node27-coverage-freshness-alert.service` 验证一次退出码。
  改过文件、手跑仍退 2 且 `reason` 一字不变 → unit 读的可能不是这份文件：做 §11.3「库侧观测失败」
  第 2 步的第一个动作（`EnvironmentFiles` / `DropInPaths`）（推导）。

**手跑 unit 的退出码怎么读**（§11 各处说"手跑一次 unit 看退出码 / 退 0 / 仍退 N"都按这一条）：
service 是 `Type=oneshot`，`systemctl --user start` 要等车道跑完才返回。`systemctl` 自己返回 0，
就是车道退了 0。车道非零时，`systemctl` 打印
`Job for … failed because the control process exited with error code` 并返回 1——那是 `systemctl` 自己的返回码，**不是**车道的退出码。车道真正的退出码
去 journal 里读：

```bash
journalctl --user -u nhms-node27-coverage-freshness-alert.service -n 30 --no-pager
```

找 `Main process exited, code=exited, status=<N>/…` 那一行，`N` 就是车道的退出码（1 告警、2 配置、3 观测）。
手跑失败与 timer tick 失败一样会触发 `OnFailure=`，**照样发一封告警邮件**——手跑不是静默的。
（systemd 对 oneshot 的这套行为是推导；journal 那行的形状在 #2468 的 node-27 receipt 里实测过，
读 `Main process exited, code=exited, status=1/FAILURE`。）

### 11.5 安装

```bash
ssh -p 32099 nwm@210.77.77.27
cd /home/nwm/NWM
# env 文件与 §10 共用；若尚未创建，先按 §10.8 建好（0600、非符号链接）。
install -m 644 infra/systemd/nhms-node27-coverage-freshness-alert.service ~/.config/systemd/user/
install -m 644 infra/systemd/nhms-node27-coverage-freshness-alert.timer   ~/.config/systemd/user/
systemctl --user daemon-reload
systemd-analyze --user verify nhms-node27-coverage-freshness-alert.service
systemctl --user start nhms-node27-coverage-freshness-alert.service   # 先手跑一次；退出码的读法见 §11.4
journalctl --user -u nhms-node27-coverage-freshness-alert.service -n 30 --no-pager
systemctl --user enable --now nhms-node27-coverage-freshness-alert.timer
systemctl --user list-timers 'nhms-node27-coverage-freshness-alert.timer' --no-pager
```

service 与 timer 都已注册进 `scripts/node27_resource_governance.py`
`DEFAULT_SERVICES`（#2466）：治理审计 receipt 的 `systemd.services.<unit>.properties` 带
它们的 `ActiveState` / `SubState`，以及（#2473 起）`LoadState` / `UnitFileState`。
**分辨"被 disable"和"从没装过"只能靠后两个**——2026-09-18 在 node-27 上用一次性 unit 实测：

| 状态 | `LoadState` | `UnitFileState` | `ActiveState` / `SubState` |
|---|---|---|---|
| timer 正常 | `loaded` | `enabled` | `active` / `waiting` |
| timer 被 `disable --now` | `loaded` | `disabled` | `inactive` / `dead` |
| 从没装过 | `not-found` | （空） | `inactive` / `dead` |

service 读 `loaded` + **`static`**，这是常态、不是故障：它没有 `[Install]` 段，由 timer 拉起，
本来就不 enable。`systemctl --user list-timers --all` **分不开**这两种情况：被 disable 的
timer 和从没装过的一样，表里一行都没有（同日实测），所以 receipt 里那张 `list-timers` 表也不能
当判据。#2473 之前生成的 receipt 没有 `LoadState` / `UnitFileState` 两个键。**这些都不是告警**：
治理审计不对 receipt 的 `systemd` 段产任何建议、不因此非零、也不发邮件，timer 被人 disable
掉不会自己冒出来。定期看治理 receipt 里的 unit 状态就是为了这个——与 §10.8 的 frontier 车道
同一口径，同样是靠人按周期读。本车道无状态、无自有 receipt、无自有日志：它自己的痕迹只在 journal 里
（`journalctl --user -u nhms-node27-coverage-freshness-alert.service`），而 journal 只能告诉你
**跑过的那些 tick** 怎么样了——timer 被 disable 之后它就不再有新行，而「没有新行」与「一切
正常、只是没告警」在那里长得一模一样。治理 receipt 是这两个 unit 出现在**清单**上的唯一地方，
这才是它在这里值一句话的原因。

单元安装是 node-27 上的**手工步骤**，`git pull` 只更新 `ExecStart` 指向的脚本本体。
`Environment=PYTHONPATH=/home/nwm/NWM` 是**双保险**，不是今天 node-27 上 import 得了
`services.tiles.mvt` 的原因：生产 venv 以 editable 方式装了本项目 `nhms`（`site-packages` 里的
`__editable__.nhms-0.1.0.pth` 把 `apps` / `packages` / `services` / `workers` 映射到 `/home/nwm/NWM/…`），
不设 `PYTHONPATH`、在 `/tmp` 下跑车道照样 `rc=0`（2026-09-18 node-27 实测）。只有 venv 被重建却没装回
本项目时，这一行才起作用（推导）。手工在 shell 里跑与 unit 同口径，也带上它：

```bash
cd /home/nwm/NWM
set -a; . infra/env/node27-frontier-alert.env; set +a
PYTHONPATH=/home/nwm/NWM .venv/bin/python scripts/node27_coverage_freshness_alert.py; echo "rc=$?"
```

**导入期失败（退 2，`reason` 以 `<异常类>: …` 开头，最常见 `ImportError:` / `ModuleNotFoundError:`，
仓库代码坏了时是 `SyntaxError:` 之类，依赖漂移时也可能是 `AttributeError:`，判据见 §11.2 退 2 那行）**：
先做导入检查，它用的就是 unit 的那个解释器：

```bash
cd /home/nwm/NWM
PYTHONPATH=/home/nwm/NWM .venv/bin/python -c 'import services.tiles.mvt'; echo "rc=$?"
```

失败时它打印完整的 traceback；告警的 `reason` 被扣成 `<error text withheld: …>` 时，这里照样是原文。
**只看最后一行**就能分支（下面四支自上而下、第一条命中即停；失败而不属于前两支的，都归第三支兜底）：2026-09-18 在 node-27 的 scratch worktree 里用生产解释器逐一做坏实测，
最后一行的异常类与消息与车道 `reason` 相同（`SyntaxError` 的文件名与行号在 `reason` 的括号里，在检查
的 traceback 里则是上面几行）。**不要**不看这一行就 `uv sync`：仓库自己的代码坏了，sync 修不了。

- **仓库代码缺陷**——最后一行是下面任一种：
  - `SyntaxError:` / `IndentationError:` / `NameError:`；
  - `ImportError: cannot import name '…' from 'services.…'`（或 `'packages.…'`），括号里的路径在
    `/home/nwm/NWM/` 下；
  - `ModuleNotFoundError: No module named 'services.…'`（或 `'packages.…'`、`'scripts.…'`）。

  实测样本（node-27 scratch worktree，生产解释器）：
  `ImportError: cannot import name 'NO_SUCH_NAME_2472' from 'services.precip.constants' (…/services/precip/constants.py)`
  与 `SyntaxError: invalid syntax`（车道 `reason` 读 `SyntaxError: invalid syntax (mvt.py, line 25)`）两种车道都退 2；
  `ModuleNotFoundError: No module named 'services.precip.no_such_module_2472'` 只做了导入检查。
  `IndentationError` / `NameError` 是推导。展示模块在模块层导入的几乎全是 `services.*` / `packages.*`，
  第三方只有 `sqlalchemy`；timer 跑的就是运维 `git pull` 进来的这份共享 checkout——坏掉的是代码
  本身，同一个错每次都会复现，**不要 sync**，那只会白白改动生产共享的 venv。先看工作树与最近的提交：

  ```bash
  cd /home/nwm/NWM && git status --porcelain
  cd /home/nwm/NWM && git log -5 --oneline -- services/ packages/
  ```

  `git status --porcelain` 有输出 → 这份 checkout 上有未提交的改动或停在一半的 pull，一并记下，
  别在生产 checkout 上 reset / stash。按代码缺陷开 issue，附上导入检查的最后一行、stderr 那行
  结构化 JSON 里完整的 `reason`，以及这两条命令的输出。本车道无状态、无 dedup，修好之前每天
  06:00 一封。
- **缺第三方依赖**——最后一行是 `ModuleNotFoundError: No module named '<名字>'`，名字不以
  `services.` / `packages.` / `scripts.` 开头（实测样本：`No module named 'no_such_thirdparty_2472'`，
  车道退 2、`reason` 同文）。先 dry-run，再 sync：

  ```bash
  cd /home/nwm/NWM && export PATH=$HOME/.local/bin:$PATH && uv sync --all-extras --dev --dry-run   # 看它要装的包里有没有缺的那个
  cd /home/nwm/NWM && export PATH=$HOME/.local/bin:$PATH && uv sync --all-extras --dev
  ```

  node-27 上这个 dry-run **从来不是空的**：2026-09-18 两次实测都报 `Would install 3 packages`
  （`mapbox-vector-tile`、`pyclipper`、`shapely`），而本车道照样 import 得了。所以判据不是"有没有
  输出"，而是列表里**有没有缺的那个包**（发行包名可能与模块名不同，例如 `psycopg2` 对应
  `psycopg2-binary`）。列表里没有 → sync 不会去动它（锁文件里没有它，或 uv 认为它已经装好），
  sync 修不了：别 sync，按「仓库代码缺陷」那支开 issue，把 dry-run 的输出一并附上（推导）。
  sync 会就地改生产共享的 `.venv`（display API、autopipe、本车道都跑在它上面），默认是精确同步、
  锁文件之外的包会被卸掉——所以只在这一支（以及下一支里版本对不上的情形）才 sync。sync 完**再验两遍**：
  重跑上面的导入检查（`rc=0`），再用 §11.4 那条 `systemctl --user start nhms-node27-coverage-freshness-alert.service`
  手跑 unit，`systemctl` 返回 0（读法见 §11.4 末尾）才算闭环。sync 之后导入检查仍失败 → 不是缺包，
  按「仓库代码缺陷」那支处理；导入检查过了、unit 仍失败 → 按下面「通过」那支对照（推导）。
- **已装的第三方包与代码对不上**（兜底：失败、而最后一行不属于上面两支的，都走这里）——典型形状：
  - `ImportError: cannot import name '…' from '<第三方模块>' (…/site-packages/…)`；
  - `AttributeError: module '<第三方模块>' has no attribute '…'`；
  - 共享库加载失败：`ImportError: <库>.so…: cannot open shared object file …`。

  实测样本（2026-09-18 node-27 scratch worktree，生产解释器）：
  `ImportError: cannot import name 'NoSuchThing2472' from 'sqlalchemy.exc' (/home/nwm/NWM/.venv/lib/python3.11/site-packages/sqlalchemy/exc.py)`
  与 `AttributeError: module 'sqlalchemy' has no attribute 'no_such_attr_2472'`，车道都退 2、`reason` 同文。
  共享库那一形状是模拟的（让 `psycopg2._psycopg` 的导入抛 `ImportError`），驱动检查的最后一行原样读
  `ImportError: libpq.so.5: cannot open shared object file: No such file or directory (simulated)`——末尾的
  `(simulated)` 是模拟时加的标记，真实的库缺失没有它；车道落在退 3、
  `reason` 读 `ImportError: <error text withheld: redaction unavailable (ImportError)>`——即 §11.3
  「非驱动观测失败」开头那条路径。先拿装着的版本对锁文件（`<包>` 是发行包名，可能与模块名不同：
  `sqlalchemy.exc` → `sqlalchemy`，`psycopg2` → `psycopg2-binary`）：

  ```bash
  cd /home/nwm/NWM && export PATH=$HOME/.local/bin:$PATH && uv pip show --python .venv/bin/python <包>
  cd /home/nwm/NWM && grep -A2 '^name = "<包>"' uv.lock
  ```

  健康时两边版本一致：`sqlalchemy` 在 venv 里是 `Version: 2.0.49`、锁文件里是 `version = "2.0.49"`，
  `psycopg2-binary` 两边都是 `2.9.12`（两条命令原样、2026-09-18 node-27 实测）。下面这套按版本分的判断是推导：
  - 版本不同 → 依赖漂移：走上一支「缺第三方依赖」的 dry-run → sync → 再验两遍。
  - 版本相同，或是共享库错误而包本身装着 → sync 修不了：要么仓库代码与锁定的依赖版本对不上，
    要么系统库坏了。**不要 sync**，开 issue，附上导入检查（或驱动检查）的最后一行与这两条命令的输出。
- **通过**（`rc=0`）→ 手工已复现不出。用 §11.4 那条 `systemctl --user start` 手跑一次 unit：`systemctl`
  返回 0（读法见 §11.4 末尾）→ 是已经过去的瞬时故障（例如 tick 撞上了一次做到一半、之后已补完的
  pull），闭环。仍失败（journal 里 `status=2/…`）→ 用 §11.4 末尾那条 `journalctl` 取这次的 `reason`，
  与导入检查对照；两边不一致说明 unit 跑的不是手工这套环境（推导）：
  - 先回 §11.3「库侧观测失败」第 2 步查 `EnvironmentFiles` 与 `DropInPaths`——`EnvironmentFile=`
    也能改 `PYTHONPATH`。
  - 那里干净，再看 editable install 还在不在（上面那段双保险失效的前提）：

    ```bash
    ls /home/nwm/NWM/.venv/lib/python*/site-packages/__editable__.nhms-*.pth
    ```

    健康时列出 `…/python3.11/site-packages/__editable__.nhms-0.1.0.pth`、`rc=0`（2026-09-18 node-27 实测）。
    文件没了，**并且** `systemctl --user show -p Environment nhms-node27-coverage-freshness-alert.service`
    也不读 `Environment=PYTHONPATH=/home/nwm/NWM`（健康值 2026-09-18 node-27 实测）→ 两道保险都没了，
    unit 导不进 `services`。这时连 `packages.common.redaction` 也导不进，`reason` 读
    `ModuleNotFoundError: <error text withheld: redaction unavailable (ModuleNotFoundError)>`（推导）。重跑本节开头安装块里的两条 `install` 与 `daemon-reload`，再用 §11.4 那条 `systemctl --user start`
    确认 `systemctl` 返回 0（读法见 §11.4 末尾）；venv 为什么丢了本项目，单独查（推导）。
  - 以上都干净（drop-in 没有、`.pth` 在，或 `Environment=PYTHONPATH=` 在），unit 却仍 `status=2`、两边仍
    对不上 → 重跑本节开头安装块里的两条 `install` 与 `daemon-reload`，再手跑一次（读法见 §11.4 末尾）。
    仍失败 → 开 issue，附上 unit journal 里那行结构化 JSON 的 `reason` 与手工导入检查的输出（`rc=0`）（推导）。

要验证真实投递链路，用 systemd drop-in 把这一次调用指到 scratch 库（**必须先用空的
`EnvironmentFile=` 清空已有列表**，`Environment=` 赢不了后读的 `EnvironmentFile=`）：

```ini
# ~/.config/systemd/user/nhms-node27-coverage-freshness-alert.service.d/scratch.conf
[Service]
EnvironmentFile=
EnvironmentFile=/home/nwm/tmp/issue2080/scratch.env
```

验完 `rm` 掉 drop-in、`daemon-reload`，用
`systemctl --user show -p EnvironmentFiles nhms-node27-coverage-freshness-alert.service`
确认已经回到生产 env 文件。
