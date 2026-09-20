# node-27 receipt — master 代码上的 publish tick（#2433）与天地图底图来源授权（#2436）

- 日期：2026-09-20，取证窗口 UTC `12:01:32Z → 12:05:42Z`（node-27 本机 `+08:00`）。
  窗口两端取 node-27 上证据文件的实际 mtime：起点是 capture 脚本落盘的 `12:01:32.05Z`
  （三张截图 `12:01:49Z` / `12:02:00Z` / `12:02:10Z`，`live/provider-summary.json` 的
  `capturedAt=2026-09-20T12:02:10.818Z`），中段是授权矩阵 `12:03:44Z → 12:03:48Z`，
  终点是 `publish-ticks.log` 落盘的 `12:05:42Z`（其内部 `captured_at` 记的是脚本启动的 `12:05:37Z`）。
- 节点：node-27（`210.77.77.27`），活动树 `/home/nwm/NWM`，取证时 `HEAD=f9da6805` 分支 `master`。
- OpenSpec change：`verify-node27-publish-tick-and-basemap-origin`。
- **本窗口对节点零写入**：只跑 `grep`/`awk` 读日志、`git reflog` 读引用、`curl` GET、Playwright GET。
  未连任何 DB、未重启任何 unit、未改任何 env、未提交任何作业、未改 provider 配置或 key。
- 复现脚本随本 receipt 入库：[`publish-ticks.sh`](publish-ticks.sh)、[`provider-matrix.sh`](provider-matrix.sh)、
  [`capture-2436.cjs`](capture-2436.cjs)。原始输出：[`publish-ticks.log`](publish-ticks.log)、
  [`provider-matrix.log`](provider-matrix.log)、[`capture.log`](capture.log)、[`live/`](live/)。
- runner md5（本地与 node-27 逐字节一致）：`publish-ticks.sh` = `84467cd671c9f8ae79bb56a389d476ff`、
  `provider-matrix.sh` = `913ddcde4e9469eadfb7843223ee2b4f`、
  `capture-2436.cjs` = `ae831e245ee6c3e3f0d13fb34caf4dab`。
- **未断言**：`https://test.nwm.ac.cn` 当时所服务的前端 bundle 的构建 SHA。这不影响 §2 的结论——
  §2 的判定面是 provider 侧的来源授权，而底图构造路径
  （`apps/frontend/src/components/map/m11MapRuntime.tsx` / `M11MapLibreSurface.tsx`）
  自 `7ecc46be` 以来无改动；但 §2.3 因此只声称「该来源上外部底图可用」，不声称 bundle 身份。

---

## 1. #2433 — master 代码上的 publish tick

### 1.1 活动树身份（谁在跑）

`git reflog --date=iso` / `git reflog show master --date=iso`（原文见 `publish-ticks.log` §3 §4）：

| CST 时刻 | 事件 | 结果 |
|---|---|---|
| 2026-09-16 15:13:17（`07:13:17Z`） | `checkout: moving from issue1987-reviewed-new-415cbd1e to master` | 活动树离开 `415cbd1e`，回到分支 `master` |
| 2026-09-16 16:15:15（`08:15:15Z`） | `pull --ff-only` | `master = 7ecc46be` |
| 2026-09-17 15:13:59（`07:13:59Z`） | `merge c9891c14: Fast-forward` | `master = c9891c14`（本地态，**不在** `origin/master` 上） |
| 2026-09-17 15:30:57（`07:30:57Z`） | 引用回退（空 reflog message） | `master` 退回 `7ecc46be` |
| 2026-09-18 09:28:37 起 | 连续 `pull --ff-only` | `a31aec64 → … → f9da6805` |

`git merge-base --is-ancestor <sha> origin/master` 逐个核过：`7ecc46be`（Merge PR #2428）、`a31aec64`、
`18f53e7f`、`4f8d9826`、`87236ca5`、`e0b08f3e`、`c57272a9`、`f9da6805` **全部在 `origin/master` 上**；
只有 `c9891c14` 不在，而它持有 `master` 的窗口是 2026-09-17 15:13:59 → 15:30:57 CST（17 分钟），
**该窗口内没有任何 publish tick**。

### 1.2 publish tick 表（`published > 0`，2026-09-15 起）

[`publish-ticks.log`](publish-ticks.log) §1 原文。「活动树」列由 §1.1 的 master reflog 按 tick 起止时间
**程序化套出**（reflog 时间换算为 UTC 后做区间匹配），跨越引用移动的 tick 用 `→` 标出两端。

> **`cycles` 列读法（容易读错，先说清）。** 它**不是**「该 tick 发布了哪些周期」，而是
> **MVT prewarm 阶段从 display API 拿到的 gfs / ifs 两个 `default_cycle` 快照**，去重后按出现序排列
> （两个值 = 两源不一致，一个值 = 两源一致，**不是**一趟横跨两个周期）。
> 链路：`scripts/node27_mvt_prewarm.py:378-379` 把 `discovery.cycle` 写进
> `per_source[<源>]["cycle"]`，该值来自 `:203` 的 `/api/v1/layers/discharge/cycles?source=…`
> 的 `default_cycle`，源集合是 `:60` 的 `PREWARM_SOURCES = ("gfs", "ifs")`。
> 它是 tick **末尾**的 post-publish 状态观测，用来看「展示面现在停在哪个周期」，
> 不能用来推断该 tick 干了什么。对照：基线 tick #1 的
> `2026-09-14T00:00:00Z, 2026-09-14T12:00:00Z` 与 #2433 body 的
> 「最新周期都停在 gfs `2026-09-14T00Z` / IFS `2026-09-14T12Z`」逐字吻合。

另一处同样容易读错的列：

> **`proc` 与 `pub` 不是分子分母。** `processed = len(run_results)`
> （`scripts/node27_autopipeline.py:2703`）是本 tick 处理的 run 数；
> `published` 是 `_publish_display_runs()` 里一条**全局**
> `UPDATE hydro.hydro_run h SET status='published' WHERE h.status='parsed' AND h.parsed_at IS NOT NULL`
> 的 `rowcount`（`scripts/node27_autopipeline.py:1449-1461`），
> 统计的是**全库**当时从 `parsed` 迁到 `published` 的行数，与本 tick 的 `processed` 不同总体。
> 下表里 rc=0 的 tick 两者相等，是因为当时没有 `parsed` 积压；**不能**据此把
> `published < processed` 读成「只发布了一部分」（见 §3 finding 1）。
> 下文的每条耗时一律用 `processed` 归一——它才是本 tick 的工作量。

| # | start (UTC) | done (UTC) | rc | elapsed_s | proc/pub | gfs/ifs `default_cycle` 快照 | 活动树（分支 `master`） |
|---|---|---|---|---|---|---|---|
| 0 | `2026-09-15T02:29:22Z` | `02:29:42Z` | 0 | 20 | 0/1 | 09-14T00Z | `415cbd1e` 期（reflog 见 #2017 receipt） |
| 1 | `2026-09-15T05:51:40Z` | `06:12:02Z` | 0 | **1222** | 38/38 | 09-14T00Z, 09-14T12Z | `415cbd1e`（#2017 的基线） |
| 2 | `2026-09-16T15:51:40Z` | `16:13:22Z` | 0 | **1302** | 38/38 | 09-14T12Z | `7ecc46be` |
| 3 | `2026-09-16T17:50:40Z` | `18:10:45Z` | 0 | 1205 | 38/38 | 09-15T00Z, 09-14T12Z | `7ecc46be` |
| 4 | `2026-09-16T19:05:40Z` | `19:27:54Z` | 0 | 1334 | 38/38 | 09-15T00Z | `7ecc46be` |
| 5 | `2026-09-16T21:24:40Z` | `21:48:09Z` | 0 | 1409 | 38/38 | 09-15T00Z, 09-15T12Z | `7ecc46be` |
| 6 | `2026-09-16T21:48:09Z` | `22:08:29Z` | 0 | 1220 | 38/38 | 09-15T12Z | `7ecc46be` |
| 7 | `2026-09-16T23:35:40Z` | `23:57:56Z` | 0 | 1336 | 38/38 | 09-15T12Z, 09-16T00Z | `7ecc46be` |
| 8 | `2026-09-16T23:57:57Z` | `2026-09-17T00:17:50Z` | 0 | 1193 | 38/38 | 09-16T00Z | `7ecc46be` |
| 9 | `2026-09-17T06:21:40Z` | `06:55:45Z` | 0 | **2045** | 65/65 | 09-16T12Z, 09-16T00Z | `7ecc46be` |
| 10 | `2026-09-17T06:55:45Z` | `07:05:08Z` | 0 | 563 | 11/11 | 09-16T12Z | `7ecc46be` |
| 11 | `2026-09-17T18:29:40Z` | `18:38:39Z` | 0 | 539 | 10/10 | 09-16T12Z | `7ecc46be` |
| 12 | `2026-09-17T18:40:40Z` | `19:17:03Z` | 0 | 2183 | 66/66 | 09-17T00Z | `7ecc46be` |
| 13 | `2026-09-18T06:30:32Z` | `07:11:35Z` | 0 | 2463 | 76/76 | 09-17T12Z | `258b06ec` |
| 14 | `2026-09-18T18:05:40Z` | `18:28:35Z` | 0 | 1375 | 38/38 | 09-18T00Z, 09-17T12Z | `258b06ec` |
| 15 | `2026-09-18T19:30:48Z` | `19:51:55Z` | 0 | 1267 | 38/38 | 09-18T00Z | `258b06ec` |
| 16 | `2026-09-19T05:55:40Z` | `06:30:56Z` | **1** | 2116 | 38/31 | 09-18T00Z | `8942722d` |
| 17 | `2026-09-19T06:30:56Z` | `06:53:36Z` | **1** | 1360 | 7/3 | 09-18T00Z | `8942722d` |
| 18 | `2026-09-19T06:53:36Z` | `07:02:41Z` | 0 | 545 | 4/4 | 09-18T00Z, 09-18T12Z | `8942722d` |
| 19 | `2026-09-19T07:25:40Z` | `07:41:05Z` | 0 | 925 | 21/21 | 09-18T00Z, 09-18T12Z | `8942722d` |
| 20 | `2026-09-19T07:41:05Z` | `07:53:21Z` | 0 | 736 | 17/17 | 09-18T12Z | `8942722d` |
| 21 | `2026-09-19T22:53:51Z` | `23:31:29Z` | 0 | 2258 | 76/76 | 09-19T00Z | `95f994a0` |
| 22 | `2026-09-20T06:42:40Z` | `07:06:39Z` | 0 | 1439 | 38/38 | 09-19T00Z, 09-19T12Z | `e0b08f3e`→`c57272a9` |
| 23 | `2026-09-20T07:06:39Z` | `07:29:42Z` | 0 | 1383 | 38/38 | 09-19T12Z | `c57272a9`→`f9da6805` |

tick #2 起每一个出现在「活动树」列里的 SHA——`7ecc46be`、`258b06ec`、`8942722d`、`95f994a0`、
`e0b08f3e`、`c57272a9`、`f9da6805`——都通过了 `git merge-base --is-ancestor <sha> origin/master`。
唯一不在 `origin/master` 上的 `c9891c14` 持有 `master` 的窗口是 `2026-09-17T07:13:59Z → 07:30:57Z`，
而该窗口前后最近的两趟 publish tick 分别在 `07:05:08Z` 结束、`18:29:40Z` 开始，
**窗口内零 publish tick**（每 10 分钟一趟的 no-op tick 照常在跑，但它们 `published=0`，不在本表内）。

### 1.3 #2433 要的值（a），与一个佐证 tick（b）

**（a）master 代码上的第一趟 publish tick** —— tick #2：

```
[2026-09-16T15:51:40Z] autopipe: start
[2026-09-16T16:13:22Z] autopipe: done rc=0 elapsed_sec=1302      processed=38 published=38
```

活动树：分支 `master` @ `7ecc46be`（`origin/master` 上的 Merge PR #2428）。
它的 `processed`/`published` 与基线 tick #1 **完全同构（38 条）**，因此是 1222 s 的严格可比对象。
该 tick 末尾的展示面快照是 gfs 与 ifs 同为 `2026-09-14T12Z`（基线末尾是 gfs `09-14T00Z` /
ifs `09-14T12Z`）；这只说明展示面前沿在这期间前移了一格，**不**说明该 tick 发布了哪个周期。

**（b）处理量首次明显跃升的 tick** —— tick #9（`processed` 由 38 跳到 65）：

```
[2026-09-17T06:21:40Z] autopipe: start
[2026-09-17T06:55:45Z] autopipe: done rc=0 elapsed_sec=2045      processed=65 published=65
```

活动树同为 `master` @ `7ecc46be`（tick 在 `07:13:59Z` 的 `c9891c14` 窗口开始前 18 分钟就已结束）。
**此处的依据是 `processed` 跃升，不是 `cycles` 列**——后者只是展示面的 post-publish 快照（见 §1.2 读法）。
单趟处理 65 个 run 说明当时有积压在被补齐，但本 receipt 不据此推断上游何时恢复。

> 上游恢复的时间点本 receipt **不下结论**。`cycles` 快照能说的只有「展示面在各 tick 末尾停在哪个周期」：
> 基线 tick #1 末尾是 gfs `09-14T00Z` / ifs `09-14T12Z`，master 上第一趟 tick #2 末尾两源都是
> `09-14T12Z`，其后逐 tick 前移至 `09-19T12Z`（tick #23）。这只是展示面前沿的推进轨迹，
> 不是「哪一趟发布了哪个周期」，也不对 #2439 的修复与上游恢复之间的因果关系作任何声称。

### 1.4 判据核对：`rc=0` 且 `elapsed_sec` 与 1222 s 同量级

`master` 上共 **12** 趟同规模（`processed=38`）tick：#2、#3、#4、#5、#6、#7、#8、#14、#15、
**#16**、#22、#23。下表列其中 11 趟，**显式排除 tick #16**——它 `rc=1`，2116 s / 55.7 s 每条
（相对基线 **+73%**），其 11 个 run 在 parse 段撞 statement timeout（见 §3 finding 1），
量的是失败重试路径而非正常发布路径，不能与基线同框比较。下面的区间与中位数断言**只覆盖这 11 趟**。

口径说明：同规模指 `processed` 相同（= 本 tick 处理的 run 数，§1.2）。
真正等价的工作量指标应是 `ingested`，但 `publish-ticks.sh` 未采集该字段；
`published` 因是全局 rowcount（§1.2）**不能**用来论证工作量相等。

按 `elapsed_sec` 升序：

| tick | elapsed_s | 每条耗时 s（`elapsed_sec / processed`） | 相对基线 1222 s |
|---|---|---|---|
| 基线 `415cbd1e` #1 | 1222 | 32.2 | — |
| #8 | 1193 | 31.4 | −2.4% |
| #3 | 1205 | 31.7 | −1.4% |
| #6 | 1220 | 32.1 | −0.2% |
| #15 | 1267 | 33.3 | +3.7% |
| **#2（第一趟）** | **1302** | **34.3** | **+6.5%** |
| #4 | 1334 | 35.1 | +9.2% |
| #7 | 1336 | 35.2 | +9.3% |
| #14 | 1375 | 36.2 | +12.5% |
| #23 | 1383 | 36.4 | +13.2% |
| #5 | 1409 | 37.1 | +15.3% |
| #22 | 1439 | 37.9 | +17.8% |

这 11 趟的区间是 **1193–1439 s**，中位数 1334 s（+9.2%）；基线 1222 s 落在区间内，
在这 11 趟加基线共 12 个值里排第 4 低。（把被排除的 #16 计入则区间上沿变成 2116 s，
见上方排除理由。）
跨规模的大 tick 用每条耗时对齐，同样落在同一带宽内：

| tick | proc/pub | elapsed_s | 每条耗时 s（按 `processed`） |
|---|---|---|---|
| #9 | 65/65 | 2045 | 31.5 |
| #12 | 66/66 | 2183 | 33.1 |
| #13 | 76/76 | 2463 | 32.4 |
| #21 | 76/76 | 2258 | 29.7（**快于基线**） |

**结论：`rc=0` 成立；`elapsed_sec` 与 1222 s 同量级（同规模 −2.4% ~ +17.8%，无数量级差），
不构成「明显劣化」，无需另开回归单。** #2017 Evidence Floor 第 7 组 7.2 第 2 条挂账项就此关闭。

### 1.5 lane 当前仍在跑

`publish-ticks.log` §2，取证前最近 6 趟（`2026-09-20T11:10:07Z … 12:02:07Z`）全是
`rc=0 elapsed_sec≈27` 的 no-op tick——没有新周期可发，不是 lane 停摆；
最新已发布周期 `2026-09-19T12Z`（tick #23 末尾的展示面快照）。
同窗口另做过一次只读 `ls /home/ghdc/nwm/object-store/canonical/gfs`，最新条目为 `2026091912`，与之一致；
**该次 listing 未随本 receipt 入库**，此处如实标注其来源。

---

## 2. #2436 — 隔离 loopback 来源的天地图底图 HTTP 403

### 2.1 provider 授权矩阵（node-27 出站只读 GET，不改 provider 侧任何配置）

[`provider-matrix.sh`](provider-matrix.sh) 从活动树 `apps/frontend/src/components/map/m11MapRuntime.tsx`
读 key，并对所有输出做 redact；**本 receipt、日志、截图、issue 评论均不含 key 值**。
原文见 [`provider-matrix.log`](provider-matrix.log)（`captured_at=2026-09-20T12:03:45Z`，瓦片固定 `x=3&y=1&l=3`）。

| # | UA | Referer | 图层 | HTTP | provider code / msg |
|---|---|---|---|---|---|
| A1 | curl 默认 | 无 | `vec_w` | **403** | `301012` 权限类型错误（Key 权限类型为:浏览器端） |
| A2 | curl 默认 | `http://127.0.0.1:18023/` | `vec_w` | **403** | `301012` 同上 |
| A3 | curl 默认 | `https://test.nwm.ac.cn/` | `vec_w` | **403** | `301012` 同上 |
| B1 | 浏览器 | 无 | `vec_w` | **200** | image/png 4769 B |
| B2 | 浏览器 | `http://127.0.0.1:18023/` | `vec_w` | **403** | `301007` **域名不匹配** |
| B3 | 浏览器 | `https://test.nwm.ac.cn/` | `vec_w` | **200** | image/png 4769 B |
| B4 | 浏览器 | `http://127.0.0.1:18023/` | `cva_w`（中文注记） | **403** | `301007` **域名不匹配** |
| B5 | 浏览器 | `https://test.nwm.ac.cn/` | `cva_w`（中文注记） | **200** | image/png 213 B |
| C1 | 浏览器 | `https://test.nwm.ac.cn/` | `vec_w`，伪造 key | 403 | `301001` 非法 key |
| C2 | 浏览器 | `https://test.nwm.ac.cn/` | `vec_w`，缺 `tk` | 418 | HTML 拦截页（非 provider 授权面；body 被脚本截断至 120 字符，未保留具体拦截器标识） |

A 组与 C 组是**对照**：A 证明 key 权限类型为浏览器端（非浏览器 UA 一律 `301012`，与来源无关），
C 证明 key 本身有效（伪造 key 返回的是另一个码 `301001`）。
B 组是**判定组**：同一浏览器 UA、同一 key、同一图层，只有 `Referer` 不同，
loopback 来源得 `301007 域名不匹配`，公网来源得 200。

### 2.2 归因结论

**HTTP 403 可归因：provider key 的域名白名单不含隔离 loopback 来源 `127.0.0.1`，
provider 自报错误码 `301007 域名不匹配`。** 不是 key 失效（`301001` 是另一个码，见 C1）、
不是配额、不是 provider 服务故障（同一时刻同一 key 在公网来源上返回 200）、
不是应用缺陷（应用只是按既有 source-error 契约诚实回显 provider 的失败）。

issue #2436 body 里「隔离来源 `http://127.0.0.1:18023` 可能不在 provider 允许的
referer/origin 范围内」这一假设**成立**，现有 provider 错误码为证。

### 2.3 许可来源上的真实浏览器复核（非伪造 Referer）

[`capture-2436.cjs`](capture-2436.cjs)（Playwright chromium，node-27 本机，`BASE=https://test.nwm.ac.cn`）。
脚本对每个请求断言方法为 GET/HEAD——**对生产只读，零写入**；
provider URL 从不落日志，只记 `{host, T, status}`。原文见 [`capture.log`](capture.log)
与 [`live/provider-summary.json`](live/provider-summary.json)。

| basemap | 底图层 | 注记层 | provider 请求数 | 状态码集合 | `m11-map-source-error` | attribution | 水文组 / 图例 / 降水开关 |
|---|---|---|---|---|---|---|---|
| `vector` | `vec_w` ×15 | `cva_w` ×15 | 30 | `[200]` | **不可见** | `© 天地图 \| MapLibre` | 可见 / 可见 / 可见 |
| `terrain` | `ter_w` ×15 | `cta_w` ×15 | 30 | `[200]` | **不可见** | `© 天地图 \| MapLibre` | 可见 / 可见 / 可见 |
| `satellite` | `img_w` ×15 | `cia_w` ×15 | 30 | `[200]` | **不可见** | `© 天地图 \| MapLibre` | 可见 / 可见 / 可见 |

`RESULT: PASS`。截图 [`live/basemap-vector.jpg`](live/basemap-vector.jpg)、
[`live/basemap-terrain.jpg`](live/basemap-terrain.jpg)、[`live/basemap-satellite.jpg`](live/basemap-satellite.jpg)
已逐张目检：底图与中文注记均已渲染，河网与降水叠加可见，右下角 attribution 可读，
**无错误横幅、因而也无任何含 key 的 URL 出现在截图里**（这次不需要 PR #2430 用的遮罩）。

这与 #2436 于 2026-09-16 的评论一致（当时在同一来源上观测到 30 个天地图请求全 200、
按图层分为 `vec_w` 15 + `cva_w` 15、横幅计数 0）——本节把它从一种底图扩到三种、
对每种底图各自分层统计；真正新增的是 §2.1 给出的**归因**（`301007`），
那条评论只到「差异在请求上下文」为止。

结论：**公网生产来源 `https://test.nwm.ac.cn` 上天地图外部底图健康**，
PR #2430 观察到的 403 是隔离来源独有的现象，不是公网故障。

### 2.4 受支持的验证来源

写入 [`../../node-27-bringup-checklist.md`](../../node-27-bringup-checklist.md) §C4「外部底图 provider 的受支持来源」：

- 受 provider 许可的展示来源：公网 `https://test.nwm.ac.cn`（本节 §2.3 实测 200）。
- 隔离 loopback smoke（`http://127.0.0.1:<port>`）**按设计**得 `301007`，`m11-map-source-error` 横幅可见
  是该来源的预期结果，不是应用回归。
- **但「预期」不等于「可以 PASS」**：`test:e2e:live-c4-display` 在 loopback 来源上按构造必挂——
  `apps/frontend/playwright.c4-display-lane.ts` 见 `observation.mapSourceError` 即返回
  `HOME_NOT_READY`（观测点 `apps/frontend/src/lib/c4DisplayEvidence/dom.ts:40`），
  与 `openspec/specs/c4-live-display-evidence/spec.md:48`「source error … MUST 阻止 PASS」一致。
  本单**不放松**这条闸门：本单验证过的唯一可执行做法是**换到已在白名单内的来源**，而不是改判定。
  （§2.1 B1 显示不带 `Referer` 时同一 key 也返回 200，即白名单闸只在带 `Referer` 时生效；
  但「让浏览器不发 referrer」这条路本单未测，不作为可行做法列出。）
  注意 `VITE_TIANDITU_KEY` 是**构建时**内联（`import.meta.env.*`），不是运行时开关——
  「注入白名单 key」必须连同重新构建并重新部署该来源的 bundle，且先要拿到白名单含该来源的 key；
  #2436 把它列为备选且记了维护成本，本单**未验证**该路径。
  只有不宣称 C4 PASS 的 smoke 才可以用「断言预期横幅」的写法。
- 本单不要求、也未做任何生产配置变更。

### 2.5 横幅仍诚实显示真实 source error（#2436 AC4）

**先说前件**：AC4 的原文是「**修复或配置变更后**，M11 source-error 横幅仍能诚实显示真实 source error…」。
本单**没有**做任何修复或配置变更（§2.2 的结论是应用与生产配置都无需改动），故该前件为假，
AC4 属**继承满足**而非由一份变更后 receipt 满足。下面给出的是支撑这一继承的两侧证据：

两侧都要，因为许可来源上横幅按构造不出现，光靠 §2.3 证明不了「还会显示」：

- **拒绝来源侧（横幅确实会亮）**：`docs/runbooks/receipts/2026-09-16-display-followup-batch-node27/node27-live-viewer-sanitized.log:4,6,8,10,12,14`
  在六个 viewport 全部记录 `providerErrorVisible:true`——同一份应用代码在 provider 拒绝时
  确实把失败显示出来了，且同段 receipt 记录 hydrology/precip 图层与 attribution 仍可测量。
  §2.1 B2/B4 现在补上了那次横幅背后的 provider 错误码（`301007`）。
- **许可来源侧（横幅正确地不亮）**：§2.3 三种底图全部 `providerErrorVisible:false`，
  provider 全 200——没有假阳性横幅。

代码侧零改动：`apps/frontend/src/components/map/m11MapRuntime.tsx` 与
`apps/frontend/src/components/map/M11MapLibreSurface.tsx` 在本 change 里不在 diff 内
（PR diff 中不含 `apps/**`）。非 glyph 错误仍进横幅（`m11MapRuntime.tsx:123-130,180-183`）、
glyph 错误仍降级为 console 警告。C4 判定链
（`playwright.c4-display-lane.ts` / `c4DisplayEvidence/dom.ts:40` /
`openspec/specs/c4-live-display-evidence/spec.md:48`）同样零改动。

---

## 3. 范围外发现（报告不修）

1. **2026-09-19 两趟 `rc=1`：11 个 run 因 parse 段 statement timeout 失败后靠重试自愈**
   （**不是**「部分发布」——见 §1.2 的 `proc`/`pub` 口径说明）。实测计数：
   tick #16 `processed=38, ingested=31, failed=7, published=31`；
   tick #17 `processed=7, ingested=3, failed=4, published=3`（这 7 个正是 #16 失败的那 7 个被重投）；
   tick #18 `processed=4, published=4` 把剩下 4 个收干净，`rc=0`，此后未再复现。
   同段日志 22 条
   `OUTPUT_PARSE_DB_ERROR: … canceling statement due to statement timeout`
   = 11 个失败 run × 2（同一 error 串同时进 `runs.details[]` 与 `runs.failed_runs[]`）。
   **上述 `ingested` / `failed` 计数与「22 = 11 × 2」不出自随本 receipt 入库的日志**
   （`publish-ticks.sh` 只采 `processed` / `published`）：它们取自同一窗口对 node-27 原始日志的
   另一次只读 `grep`，并与 [#2529](https://github.com/DankerMu/SHUD-NWM/issues/2529) 的代码级复核一致。
   已立单：[#2529](https://github.com/DankerMu/SHUD-NWM/issues/2529)。该单确认重试有效但
   **无退避、无计数、无告警**（`infra/systemd/nhms-node27-autopipe.service` 无 `OnFailure=`），
   本次无用户可见损伤；风险在同形状但**不自愈**的那一次。与 #2433 / #2436 无关，报告不修。
2. **天地图 key 硬编码在前端源码**：`apps/frontend/src/components/map/m11MapRuntime.tsx:50`
   的缺省 key 仅可由**构建时**的 `VITE_TIANDITU_KEY` 覆盖。该 key 的权限类型是「浏览器端」且带域名白名单
   （本 receipt §2.1 的 A 组与 B 组即为证），按构造就是客户端可见凭据，因此这是卫生问题而非泄漏。
   #2436 的边界明确把「独立的 credential/security 议题」列为 out of scope，故本单只记录不处理。

## 4. 本地闸门

| 闸门 | 结果 |
|---|---|
| `uv run ruff check .` | All checks passed!（排除仓外未跟踪的本地 `agents/`、`hooks/`、`skills/`、`settings.json`，它们不入库、不进 CI） |
| `openspec validate verify-node27-publish-tick-and-basemap-origin --strict --no-interactive` | valid |
| `scripts/select_ci_tests.py --changed-file <本 PR 16 条改动路径>` | 空集合、exit 0 —— 纯 docs/spec，后端 pytest 按 CI 路径 scope 不触发 |
| `markdownlint-cli2`（三份改动 md，CI 同 glob `docs/**/*.md`） | 0 issues |
| key 扫描：本 PR 新增/修改路径中 grep provider key 字面量 | 0 行（唯一预先声明的允许出现点是 `apps/frontend/src/components/map/m11MapRuntime.tsx:50`，本 PR 不触碰该文件） |

截图不受 grep 覆盖：本次取证在许可来源上进行，横幅未出现，因而不存在含 key 的 URL 文本，
无需沿用 PR #2430 的横幅遮罩。

## 5. 回滚

本窗口对 node-27 零写入，无可回滚项。文档改动随 PR revert 即可。
