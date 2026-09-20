# Tasks

## 1. #2433 — 补记 master 代码上的 publish tick

- [x] 1.1 在 node-27 只读扫 `/home/nwm/autopipe-logs/autopipe.log`，列出 2026-09-15 起所有
      `published > 0` 的 tick：起止时间戳、`processed` / `published`、`rc`、`elapsed_sec`、
      该 tick 内出现过的 `cycle` 值。脚本随 receipt 入库。
- [x] 1.2 用 `git reflog show master --date=iso` 与 `git reflog --date=iso` 程序化套出每个 tick
      运行时活动树的分支与 SHA（reflog 时间换算 UTC 后区间匹配，不手算），并逐个核对该 SHA 在
      `origin/master` 上（`git merge-base --is-ancestor`）。不在 `origin/master` 上的引用窗口
      必须显式声明该窗口内有无 tick。
- [x] 1.3 判据核对：`rc=0`，且 `elapsed_sec` 与 `415cbd1e` 上的 1222 s 同量级。
      比较必须同规模（同 `published` 条数）才成立；跨规模只能比每条耗时。
      **FAIL 分支**：若 `rc≠0`，或同规模 `elapsed_sec` 出现数量级差（「明显劣化」），
      按 #2433 body 的规定不在本单修，改为另开回归 issue 并把编号写进 receipt 与 #2433 评论。
- [x] 1.4 把「第一趟 master 代码上的 publish tick」与「第一趟携带上游新周期的 publish tick」
      分别记入 receipt；对 `docs/runbooks/receipts/2026-09-16-display-v2.md:145` 采取
      **追加不改写**：原「取不到」段落逐字保留（它是当时的事实），在其后追加一个引用块记补记结果
      与新 receipt 链接，并声明 7.2 第 2 条挂账项关闭。
- [ ] 1.5 把同一结论作为评论贴到 #2433（issue body 指定该处为可接受落点）。
- [x] 1.6 **deferral 路由**：2026-09-19 两趟 `rc=1` 的部分发布（`OUTPUT_PARSE_DB_ERROR`
      statement timeout）是范围外发现，按「deferrals routed — never silent」由 `issue-scribe`
      去重后立单，issue 链接回填 receipt §3 finding 1 与工作说明。
      → 已立单 [#2529](https://github.com/DankerMu/SHUD-NWM/issues/2529)；该单同时纠正了本单
      receipt 初稿把 `published < processed` 读成「部分发布」的口径错误（两者不同总体，
      见 receipt §1.2 的说明），纠正已落入 §1.2 与 §3 finding 1。

## 2. #2436 — 查明隔离 loopback 来源的天地图 403

- [x] 2.1 在 node-27 跑 provider 授权矩阵：`User-Agent`（curl 默认 / 浏览器）×
      `Referer`（无 / `http://127.0.0.1:18023/` / `https://test.nwm.ac.cn/`）×
      图层（vector base `vec_w` / 中文注记 `cva_w`），外加非法 key 与缺 key 两条对照。
      记录 HTTP 状态、响应体大小、provider `code`/`msg`。
- [x] 2.2 给出 403 的可归因结论（provider `code` 级别），并明确区分
      「隔离 loopback 来源」与「provider 许可来源」的行为。
      **FAIL 分支**：若矩阵不能把 403 归到某一个 provider 错误码（例如各组同码、或码含义含糊），
      按 #2436 AC1 允许的「带证据的 inconclusive」结案：写明已排除项、剩余假设，
      并把 provider/account owner 核查列为后续动作，不得强行给出归因。
- [x] 2.3 在 provider 许可来源上用真实浏览器（Playwright，非伪造 Referer）复跑：
      三种底图各自的底图层与注记层请求状态、`m11-map-source-error` 可见性、
      天地图 attribution、水文/降水图层可观测性，并出截图。
      **FAIL 分支**：若许可来源同样失败，按 #2436 AC2 记录 provider/account remediation owner
      与脱敏响应对比，不在本单改生产配置。
- [x] 2.4 把受支持的来源与配置写入 `docs/runbooks/node-27-bringup-checklist.md` §C4，
      并**点名** `test:e2e:live-c4-display`（`apps/frontend/package.json:18`，来源由
      `PLAYWRIGHT_LIVE_BASE_URL` 决定）：该 lane 在 loopback 来源上按构造必挂
      （`apps/frontend/playwright.c4-display-lane.ts` 见 `observation.mapSourceError` 即
      `HOME_NOT_READY`），故 C4 必须跑在 provider 白名单来源上；**不得**为此放松该闸门。
- [x] 2.5 key 卫生：矩阵脚本从活动树源码读 key 并对所有输出做 redact。
      **扫描范围限定为本 PR 新增/修改的路径**（不是全仓——key 字面量本就 checked in 于
      `apps/frontend/src/components/map/m11MapRuntime.tsx:50` 并随公开 bundle 发布，
      那是**唯一预先声明的允许出现点**，本 PR 不触碰该文件）。命令与期望：

      ```
      git diff --name-only origin/master...HEAD | xargs grep -l '<key literal>'   # 期望 0 行输出
      ```

      截图不受 grep 覆盖，由 capture 时保证：许可来源上横幅不出现，因而不存在含 key 的 URL 文本；
      若在拒绝来源取证则须沿用 `docs/runbooks/receipts/2026-09-16-display-followup-batch-node27/capture.cjs:46-50`
      的横幅遮罩。
- [ ] 2.6 把结论作为评论贴到 #2436。

## 3. 闸门

- [x] 3.1 `uv run ruff check .`
- [x] 3.2 `openspec validate verify-node27-publish-tick-and-basemap-origin --strict --no-interactive`
- [x] 3.3 `scripts/select_ci_tests.py --changed-file <本 PR 改动路径清单文件>`；返回集合非空则原样跑，
      为空则记录「纯 docs/spec，后端 pytest 按 CI 路径 scope 不触发」。
- [x] 3.4 `markdownlint-cli2 "docs/**/*.md"`（CI 的 `Markdown Lint` job 同一 glob）。
- [x] 3.5 task 2.5 的 key 扫描命令，期望 0 行。
- [ ] 3.6 CI 绿。

## Must-preserve（下游消费者，评审必看）

改这三处任何一处的语义都会把「诚实显示 source error」变成「可以宣称 PASS」，本 change 一处都不动：

| 面 | 位置 | 必须保持 |
|---|---|---|
| C4 判定代码 | `apps/frontend/playwright.c4-display-lane.ts`（`observation.mapSourceError` → `HOME_NOT_READY`） | 见到横幅即判失败，不得放宽 |
| C4 DOM observer | `apps/frontend/src/lib/c4DisplayEvidence/dom.ts:40` | 仍以 `[data-testid="m11-map-source-error"]` 为观测点 |
| C4 规格 | `openspec/specs/c4-live-display-evidence/spec.md:48` | 「source error MUST 阻止 PASS」；本 change 的 spec delta 写明 carve-out，不与之冲突 |
| 横幅本体 | `apps/frontend/src/components/map/m11MapRuntime.tsx:123-130,180-183` | 真实 source error 照旧回显，glyph 错误照旧降级为 console 警告 |

## Risk Packs

| Pack | 选中 | 落点 |
|---|---|---|
| Public API / CLI / script entry | 否 | 不新增也不改任何入口；receipt 内两个脚本是一次性取证脚本，不被任何 lane 调用。 |
| Config / project setup | 否 | 不改 `infra/env/**`、不改 systemd unit、不改 provider key。**显式非目标**。 |
| File IO / path safety / overwrite | 否 | 只新增 receipt 目录并改两份既有 md 的指定小节，无覆盖/删除语义；§8 明确追加不改写（task 1.4）。 |
| Schema / columns / units / field names | 否 | 无 schema 面。 |
| Auth / permissions / secrets | **是** | 天地图 client key 的取证卫生：脚本从源码读取并 redact，扫描范围与允许出现点见 task 2.5，闸门 3.5。 |
| Concurrency / shared state / ordering | 否 | node-27 上全部操作只读（grep / git reflog / curl GET / 浏览器 GET），无写、无重启、无 DB 连接。 |
| Resource limits / large input / discovery | 否 | 单次 awk 扫 220 MB 日志，只读且不落盘中间态。 |
| Legacy compatibility / examples | **是** | 既有 C4 判定链是本 change 文档措辞的下游消费者，见上「Must-preserve」表与 spec delta 的 carve-out；task 2.4 点名不得放松该闸门。 |
| Error handling / rollback / partial outputs | 否 | 不改 `m11-map-source-error` 横幅行为。**显式非目标**：不得为隐藏横幅改错误处理。 |
| Release / packaging / dependency compatibility | 否 | 无依赖变更。 |
| Documentation / migration notes | **是** | 本 change 的主体即文档：新 receipt + §8 追加 + §C4 受支持来源；落点见 task 1.4 / 2.4。 |

## 显式非目标

- 不修复 PR #2430 的 layout / timeline / catalog / lineage 面。
- 不声称任何未经证据的公网生产故障，也不把 loopback-only 结果升级为公网结论。
- 不改生产 provider 配置、不申请或轮换 key、不改前端源码里的缺省 key。
- 不追查 2026-09-19 两趟 `rc=1` tick 的根因——**但不沉默**：按 task 1.6 立单路由。
- 不把「隔离来源上的预期横幅」变成 C4 可以 PASS 的理由（spec delta carve-out）。
