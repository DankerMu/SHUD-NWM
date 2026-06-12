# Issue #414 Worklog — Migrate frontend API consumers on node-27 (E3-N27-01)

## Roles
- 编排/验证: Claude Code（本地）
- 验证 oracle: 本地 `check:api-types` + `build`；node-27 build receipt（display_readonly）

## 状态评估结论（archetype 重判）
原 issue 2026-06-10 标注「⏸ Blocked-by-design」，前置 E3 后端轨 #411/#412/#413/#415。
当前（2026-06-12）：**#411/#412/#413 全部 CLOSED**（E3 后端轨已落地，本地已 ff-only 同步）。

读 #411 退役清单 + #412 弃用策略后判定：**#414 不是代码迁移 issue，是 closeout/证据 issue**：
- #412 政策明确：**no current endpoint deprecated/removal-ready，且选定零 replacement endpoint**。
- #411 清单：`latest-product`、canonical `forecast-series` 均为 `active` 兼容契约，非 removal-ready。
- 前端 consumers（`bootstrap.ts`/`stores/forecast.ts`/`stores/overviewData.ts`/`lib/hydroMet/riverForecast.ts` + hydroMet popup/store 扇出）全部调用**规范活跃端点**，无 removal-candidate 可迁。
- → 镜像 #413 后端范式（task 3.4：无 candidate consumer → 记录证据 + explicit defer）。

## 验证证据（orchestrator grep，已核）
- F3 `client.GET('/api/v1/...')` 实际只调用：`mvp/qhh/latest-product` + `basin-versions/{...}/river-segments/{...}/forecast-series`（+ 其它无关业务端点）。无 removal-candidate / shorthand。
- F4 docs-only shorthand 裸 `/api/v1/river-segments/{segment_id}/forecast-series`：**0 命中**（全部为规范 basin-versions 前缀）。

## 交付物
- `docs/governance/API_CONTRACT_FRONTEND_CONSUMER_EVIDENCE.md`（explicit deferral 证据，镜像 #413）
- E3 tasks.md 勾选 3.2 / 3.3 / 5.14；补 #414 expected outputs + 5.15/5.16 验证项
- 无 `apps/frontend/src` 运行时改动（git status 仅限 governance 证据/docs/OpenSpec）

## 动态阶段状态
- [x] 状态评估 + archetype 重判（closeout，非 migration）
- [x] grep 证据核验
- [x] 证据文档 + tasks.md 勾选
- [x] check:api-types + build（本地绿 + node-27 receipt @e4d70eb 绿 16.57s）
- [x] cross-review（rev414-evidence）→ verify gate
- [ ] PR + 关闭 issue

## 候选/裁决账本
- C1 [rev414-evidence F1/F2] 称证据文档不存在/tasks 未勾选 → **REFUTED**：分支竞争假阴性（评审期间本地工作树被切到 #432 分支）。`git show e4d70eb --stat` 证实交付物确在 #414 commit（doc 151 行 + 3.2/3.3/5.14 勾选 + 零 apps/frontend/src 改动）。
- C2 [rev414-evidence F3/F4/F5/F6] **CONFIRMED**：closeout archetype 正确；#412 零 replacement；前端零 removal-candidate/零裸 shorthand 调用；consumer 属实；零运行时改动。
- 终态：**CONFIRMED-CLOSEABLE**。

## 决策
- D1: #414 判为 closeout/explicit-deferral，不改前端运行时代码——直接遵循 #412 政策（never break userspace：政策禁止在无 replacement 时迁移/收缩）。
- D2: 用户 2026-06-12 指令「解决 #414」= 解除其本人 2026-06-10 的 blocked-by-design 暂缓；前置已闭合，按 closeout 收口。
