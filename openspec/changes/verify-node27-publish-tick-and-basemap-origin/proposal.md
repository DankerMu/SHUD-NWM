# 补记 master 代码上的 publish tick，并查明隔离 loopback 来源的天地图底图 403

## Why

两条挂账的 node-27 验证债，都只能用实机观测关掉，都不改运行时代码：

- **#2433**：#2017 的 Evidence Floor 要「一次 publish tick 的 `autopipe: done rc=… elapsed_sec=…`」。
  2026-09-16 的部署窗口里镜像与 DB 最新周期停在 gfs `2026-09-14T00Z` / IFS `2026-09-14T12Z`
  （停滞本身见 #2432，根因由 #2439 修复），窗口内 4 次 tick 全是 no-op，
  `docs/runbooks/receipts/2026-09-16-display-v2.md:145` 按实况记了「master 代码上尚无 publish tick」。
  该挂账项在 #2433 里有归属，需要在 lane 恢复后补记实测值。
- **#2436**：PR #2430 的隔离 live browser verification 里，天地图 vector 底图请求返回 HTTP 403，
  `m11-map-source-error` 横幅在六个 viewport 全部可见
  （`docs/runbooks/receipts/2026-09-16-display-followup-batch-node27/node27-live-viewer-sanitized.log:4,6,8,10,12,14`）。
  当时只确认了隔离来源的事实，未确认 provider 侧归因，也不能据此推出公网生产故障或健康。

两条都是「在 node-27 上取证 + 把结论写回文档」，共享同一次上机窗口与同一份 receipt，故合并为一个 change。

## What Changes

- 新增 receipt `docs/runbooks/receipts/2026-09-20-node27-publish-tick-and-basemap-origin/`：
  publish tick 表（含 cycle 列与活动树身份）、天地图授权矩阵（key 全程 redact）、
  公网来源的真实浏览器证据（三种底图 × 底图/注记层 × 状态码 + 截图）。
- `docs/runbooks/receipts/2026-09-16-display-v2.md` §8 **追加**（不改写）一段补记：
  原「取不到」段落逐字保留——它记录的是当时的事实——其后追加补记结果与新 receipt 链接。
- `docs/runbooks/node-27-bringup-checklist.md` §C4 新增「外部底图 provider 的受支持来源」小节：
  隔离 loopback smoke 看到 `m11-map-source-error` 是 **provider key 域名白名单的预期结果**，
  不是应用回归；受 provider 许可的展示来源是公网 `https://test.nwm.ac.cn`。
  该小节同时点名 `test:e2e:live-c4-display` 必须跑在白名单来源上——
  `apps/frontend/playwright.c4-display-lane.ts` 见到 `m11-map-source-error` 即判 `HOME_NOT_READY`，
  这条闸门不得为本单放松。
- 一条 `responsive-screenshot-evidence` spec delta，把「live browser 证据必须声明其来源相对外部 provider
  的授权状态」固化，并写明 carve-out：**不放松** `c4-live-display-evidence` 的
  「source error MUST 阻止 PASS」，免得 `openspec archive` 把矛盾折进主 specs。
- **不动任何运行时代码**：不改 `m11MapRuntime.tsx` / `M11MapLibreSurface.tsx`，
  不为隐藏横幅改错误处理，不改生产配置，不改 provider key。

## Triage

```text
Issue type: test
Fixture level: none
Upstream suggested level: absent (无上游 Suggested fixture level；按 issue-risk-contract「docs / 无运行时行为」判 none)
Blast radius: 仅文档与 receipt，无运行时代码路径。但错的措辞有真实后果：§8/§C4 写错会让
             #2017 的 Evidence Floor 与 #2436 的分诊结论建立在错误实测值上；§C4 若把
             「预期横幅」写成可接受态，会误导 C4 来源选择，把一次真 source error
             读成已声明的预期而宣称 PASS（下游消费者见 tasks.md「Must-preserve」表）。
Selected risk packs: Documentation / migration notes；Auth / permissions / secrets（取证输出必须 redact provider key）；
             Legacy compatibility（既有 C4 source-error 判定链是本文档措辞的下游消费者）
Evidence floor: node-27 实测 publish tick 表（rc / elapsed_sec / processed / published / cycles / 活动树 SHA）；
             node-27 天地图授权矩阵（UA × referer × 图层，key redacted）；
             公网来源真实浏览器 receipt（provider 全 200、无错误横幅、attribution 可见）；
             本地 `uv run ruff check .`、`openspec validate <change> --strict --no-interactive`、
             受影响 md 的 markdownlint；受提交路径影响的 `scripts/select_ci_tests.py` 选择集。
```

> provider key 的性质：它是**浏览器端、带域名白名单**的公开客户端凭据，本就随前端 bundle 发布
> （`apps/frontend/src/components/map/m11MapRuntime.tsx:50`）。因此本单的 redact 纪律是
> **#2436 自己定的 issue 边界规则**（「不得在 issue、log、截图中复制 client key」），
> 不是保密控制，也不声称这是一次泄漏。

`design.md` 省略：本 change 是 `none` 级、零运行时改动的取证与文档回写，没有需要展开的变更面或不变量。
