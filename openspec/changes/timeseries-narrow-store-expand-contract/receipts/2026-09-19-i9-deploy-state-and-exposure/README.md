# I9 部署态核查与 legacy 表实况（2026-09-19）

只读。**零生产写。** node-27 `nhms-db` :55432，
`PGOPTIONS='-c default_transaction_read_only=on -c statement_timeout=…'` + `conn.set_session(readonly=True)`，
DSN 经 `infra/env/node27-ingest.env` 由 env 传入，**未进 argv、未落本收据**。

本收据的头两版把两条关键行为事实写反了，改正过程记在 §8，不删。

## 1. 更正：6.3 已经在树上，不是待办

上一份收据（`../2026-09-18-i9-entry-gate/`）记 “node-27 当时在 `258b06ec`，未 pull”。**该状态已过期。**

```
git reflog（node-27 /home/nwm/NWM）
8942722d HEAD@{2026-09-19 08:57:22 +0800}: pull -q --ff-only origin master: Fast-forward
258b06ec HEAD@{2026-09-18 13:56:11 +0800}: pull --ff-only: Fast-forward
```

`8942722d` 是 #2499（node-27 维护服务恢复）的合并，其历史含 `bd06c6dd`（本 change 的 6.3）。
工作树干净（`git status --porcelain` 为 0 行），9 条 stash 均为历史遗留、未动。

我先前把 “部署 6.3” 列为生产 GO 的第一步，**基于过期状态，判断错误**。

该次 pull 由会话执行，非定时器：三个含 `git pull` 字样的 unit
（`nhms-node27-timeseries-retention` / `-coverage-freshness-alert` / `-resource-governance`）
里 `git pull` **只出现在注释**（“`git pull` updates only the ExecStart payload”），
`ExecStart` 均直接指向 `/home/nwm/NWM/scripts/…`。**node-27 上没有任何自动 pull 机制**，
`crontab` 仅有 acme.sh 续证。故 “预授权merge” 的爆炸半径仍是 “需有人去 pull”。

## 2. 部署未完成：树是 6.3，进程还不是

| 面 | sha | 时刻 |
|---|---|---|
| git 工作树 | `8942722d`（含 6.3） | 2026-09-19 08:57:22 pull |
| `nhms-display-api.service` **进程** | `258b06ec`（6.3 之前） | 2026-09-18 13:56:11 启动，**此后未重启** |

`ExecMainStartTimestamp=Fri 2026-09-18 13:56:11 CST`，早于 pull 近 19 小时；uvicorn 不因 `git pull` 换掉已加载代码。
写/解析侧（`nhms-node27-autopipe.timer` 等每次从工作树现取脚本）已是 6.3，读侧进程仍是 6.3 之前。

**该劈裂态对用户行为中性**（§5 证明），但它未被任何收据声明，且 `DROP COLUMN` 会与它冲突（§7）。

## 3. 门仍然开着

| 指标 | 值 |
|---|---|
| **窗口内（21d）legacy 路由 run** | **0** |
| 窗口外 legacy 路由 run | 2919（published 2685 + superseded 234） |

与 `../2026-09-18-i9-entry-gate/` 一致（两处独立取数）。6.2 的 refuse 条件不成立。

## 4. legacy 表实况

两张表的时间维**都是 `valid_time`**（`timescaledb_information.dimensions`）：
narrow `1 day`，legacy `3 days`（实际残留 chunk 为 7 天跨度）。

| chunk | 范围（valid_time） | 压缩 | reltuples | 大小 |
|---|---|---|---|---|
| `_hyper_3_91_chunk` | 2026-08-27 → 09-03 | 是 | 9.08×10⁸ | 48 kB |
| `_hyper_3_107_chunk` | 2026-09-03 → 09-10 | 是 | 1.06×10⁹ | 48 kB |
| `_hyper_3_110_chunk` | 2026-09-10 → 09-17 | **否** | 9.78×10⁸ | **558 GB** |
| `_hyper_3_113_chunk` | 2026-09-17 → 09-24 | **否** | 1.94×10⁸ | **108 GB** |

`hypertable_size` 合计 **717 GB**（narrow 同期 468 GB）。
精确 `count(*)` **本次未测量**：该全扫在压缩 hypertable 上超 15 分钟无结果，已主动终止，改用 `reltuples` / `approximate_row_count`。

## 5. 核心事实一：2919 个 legacy 路由 run 的事实**已经没了**，且不是重启造成的

```
SELECT count(*) FROM hydro.river_timeseries_legacy WHERE valid_time < '2026-08-27 00:00+00';
  -> 0
```

legacy 路由 run 的 `end_time` 全部 ≤ 2026-08-25，故其事实的 `valid_time` ≤ 08-25 < 08-27，
而 legacy 表**最老的残留 chunk 从 08-27 起**——能装下它们的 chunk 早已被保留期 `drop_chunks` 删除。

另一侧同样为空：

```
SELECT count(*) FILTER (WHERE EXISTS (SELECT 1 FROM hydro.river_timeseries t WHERE t.run_key=h.run_key))
  FROM hydro.hydro_run h WHERE h.timeseries_store='legacy';
  -> 2919 中 0 个有窄表事实
```

**结论：这 2919 个 run 的事实在 legacy 表和窄表里都不存在，早已全暗。**
可见性丢失由**保留期**在数日前完成，`spec.md:54` 所谓 “the same visibility loss retention imposes” 在此已是完成时。
display API 重启到 6.3 **不改变这些 run 的任何可见性**——新旧读方都取不到数据。

## 6. 核心事实二：那 717 GB 是双写冗余，DROP 无损

对最新 legacy chunk 取 1 小时切片（`valid_time ∈ [2026-09-20 00:00, 01:00)`）：

| 表 | 行数 | distinct `run_key` |
|---|---|---|
| legacy | 900 204 | 152 |
| narrow | 4 501 020 | 760 |

那 152 个 `run_key` 的归属与窄表存在性：

```
SELECT h.timeseries_store, h.status, count(*), count(*) FILTER (WHERE EXISTS (
         SELECT 1 FROM hydro.river_timeseries t WHERE t.run_key=h.run_key))
  FROM hydro.hydro_run h WHERE h.run_key = ANY(<152 keys>) GROUP BY 1,2;
  -> ('narrow', 'published', 152, 152)
```

**152 个全部是 narrow 路由、published，且 152 个全部在窄表里有事实。**
即 legacy 表残留的 717 GB 是 narrow 路由 run 的**双写副本**，不是任何 run 的唯一副本。
`DROP TABLE hydro.river_timeseries_legacy` 对这部分数据**无损**，释放 717 GB 冗余存储。

（容器实况：`pg_default` 1265 GB；`/data/GHDC` 15 T 用 1.9 T 余 12 T；`/` 98 G 用 76 G 余 18 G / 82%。）

**测量边界**：containment 只在上述 1 小时切片上证明（152/152），未对全表 4 个 chunk 穷举——
穷举需全扫 666 GB 未压缩数据，本次未做。另一个切片（09-18）的同类查询超时未得结果。

## 7. legacy 写方已经停了

- 工作树中 `river_timeseries_legacy` 共 10 处命中，**全部是注释、生命周期工具的表名常量、以及 000059 迁移自身**；
  `packages` / `workers` / `services` / `scripts` / `apps` / `db` 下**无任何 INSERT 指向 legacy**。
  `workers/output_parser/parser.py` 与 `scripts/node27_autopipeline.py` 中残留的 “dual-write” 字样
  是窄表写路径的错误文案措辞与一句 docstring，非实际写入。
- 90 秒内两次采样 `pg_stat_all_tables.n_tup_ins`，**33 个 river chunk 无一计数器移动**。
  （该窗口短于 autopipe 的 10 分钟周期，**不足以单独证明写入已停**；与上一条代码证据合并才成立。）
- `_hyper_3_113_chunk`（09-17→09-24）仅 1.94×10⁸ 行，远低于前一周 chunk 的 9.78×10⁸——
  与 “写入在 09-19 08:57 树切到 6.3 时停止” 一致。

## 8. 实测流量，以及一次被更正的假阴性

### 假阴性

首次查 `journalctl --user -u nhms-display-api.service`，得 “7 天 78 行、forecast-series 命中 0”，
据此几乎下了 “无真实流量” 的结论。**该结论是假的**：unit 定义为

```
StandardOutput=append:/tmp/display-api.log
StandardError=append:/tmp/display-api.log
```

uvicorn 的 access log 根本不进 journald，journald 里只有 systemd 生命周期消息。
真日志 96 MB / 741 334 行。**“0 命中” 是查错了地方，不是没有流量。**

### 实测

对 `/tmp/display-api.log` 全量提取 `run_id=` 与 `hydro.hydro_run` 对账：

| store | distinct run | 请求数 |
|---|---|---|
| legacy | 30 | 252 |
| narrow | 635 | 3507 |
| 未解析（run 已不在 `hydro_run`） | 1 | 25 |
| 合计（带 `run_id`） | 666 | 3784 |

legacy 路由 run 历史上确被真实用户请求（占带 `run_id` 请求的 6.7%），
`issue_time` 高频值 2026-08-22/23/24/26/30/31 亦落在 legacy 区间。

**但这不构成重启风险**：由 §5，这些 run 的事实早已不存在，这 252 次请求是数据尚在时打的。

**测量边界**：uvicorn access log 行**不带时间戳**，且 unit 用 append 模式跨重启累积，
故上述计数是该日志文件生命期内的累计量，**不能换算为 “近 7 天”**，也无法判定这 252 次中有多少发生在数据被保留期删除之后。

### 本收据前两版的两处不实断言（已更正，留痕）

1. “旧读方严格优于重启后：legacy run 路由到 legacy 表，正确” —— **假**。
   旧读方确实路由到 legacy 表，但**表里已无这些 run 的行**（§5）。两个读方都返回空。
2. “display API 一重启到 6.3，这 30 个 run 的曲线即刻取不到数据” —— **假**。
   它们**已经**取不到了。我把保留期数日前完成的丢失，错记成了重启将要触发的丢失。

两条都属于 “断言未读过的行为事实” ——与受票 C3 同一失败类。
第一版写作时我在推理中留了 “Let me not over-infer” 的自我提醒，随后仍把断言写了下去；
是复核者指出 §4 的 chunk 跨度（08-27→09-24）与 §3 的 run `end_time`（≤08-25）自相矛盾才暴露。

## 9. 对 6.2 的输入

- **门开着**：窗口内 legacy 路由 run = 0（§3）。
- **DROP 无损**：残留 717 GB 是 narrow 路由 run 的双写副本，切片内 152/152 在窄表有对应（§6）。
- **无活写方**（§7），故 `DROP TABLE` 不会与写入者争锁。
- **`DROP COLUMN hydro_run.timeseries_store` 需要 ACCESS EXCLUSIVE**，
  而当前 display API 进程（`258b06ec`）**仍在读该列**。迁移前必须先重启 display API 到 6.3，
  否则该进程会阻塞锁、并在 DROP 后对每个请求抛 `UndefinedColumn`。
  `spec.md:54` 的 “deploy routing-free code before the migration” 指的是**进程**，不是工作树。
- **yd 不受影响**：`/home/nwm/yd-NWM`（display API :8081，自有 timer）连的是 **:55434**，
  非生产 `nhms-db` :55432；且其树中 `timeseries_store` 命中 **0**。
  “yd的不停” 照旧：本 change 不触碰任何 `yd-*` 单元。

## 10. display API 重启：6.3 进程化 + live receipt（本收据唯一的生产写动作以外的状态变更）

§2 的劈裂态已结清。**只重启 `nhms-display-api.service`，`yd-*` 一个未碰**
（yd `MainPID=3163766` 重启前后未变、仍 `running`）。

```
重启前 MainPID=1058434  ActiveEnterTimestamp=Fri 2026-09-18 13:56:11 CST  (258b06ec)
重启后 MainPID=2341214  ActiveEnterTimestamp=Sat 2026-09-19 09:44:48 CST  (8942722d / 6.3)
```

同一组探针，重启前后各跑一次（CLAUDE.md C1–C4）：

| 探针 | 重启前（`258b06ec`） | 重启后（6.3） |
|---|---|---|
| narrow run 曲线（`fcst_gfs_2026091800_dg_0883c7e9…`） | `200` / 6070 B / **169 点** / 92.57 s | `200` / **6070 B / 169 点** / **0.15 s**（复测 0.30 s） |
| legacy 路由 run 曲线（`fcst_gfs_2026081812_dg_f14b5404…`） | `404` / 0 点 | `404` / 0 点 |
| `/`（单图展示） | `200` | `200` |
| `/ops` | `200` | `200` |
| deny-write `POST /api/v1/river-networks` | `401` | `401`（附完整 policy audit 记录，`no_mutation_expected: True`） |

启动后日志无异常。

**两条判读：**

1. **legacy 曲线重启前就是 `404`。** 这是 §5、§8 两处更正的**实测确证**，不再只是推理：
   旧进程（仍带两表路由）对该 run 同样取不到数据。重启对 legacy 路由 run **行为中性**，已实测。
2. **narrow 输出重启前后逐字节相同**（6070 B / 169 点），**耗时 92.57 s → 0.15 s**。
   合理解释是 6.3 删掉的两表路由腿此前要扫 §4 那个 558 GB 未压缩 legacy chunk。
   **但本收据不给出倍数结论**：重启前仅 1 个样本且未控冷缓存，
   不足以把全部差值归因于路由腿。此处只记两侧实测值与样本数（前 n=1，后 n=2）。

## 11. 越界项（report, don't fix）

- `nhms-display-api.service` 把 stdout/stderr append 到 **`/tmp/display-api.log`**，现已 96 MB，无轮转无上限，
  且落在 `/`（98 G，已用 82%）。CLAUDE.md 明确记过 `/tmp` 塞满根卷的事故（#1765）。
- `systemctl --user list-units 'nhms*'` 下十余个一次性 issue 单元长期驻留 `failed`
  （#1987 window ×2、#2349 catchup ×2、#2374、#2382 reparse pilot、pgdata prepare ×2、reslice ×4、resource-governance），
  与本 change 无关，但会污染任何 “有没有东西挂了” 的判断。
- legacy 表两个最大的残留 chunk（558 GB + 108 GB）**未压缩**，而 spec 要求
  “Both tables SHALL be compressed under the same lag”。压缩 timer 上次运行 09-18 12:25。
  本 change 不处理（DROP 在即），但若 6.2 推迟，这是 666 GB 本应压缩而未压缩的空间。
