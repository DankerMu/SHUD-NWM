# 展示端移动形态真机确认 receipt（2026-10-10，#2818）

OpenSpec change `mobile-responsive-display` task 7.3。清单见 `docs/runbooks/display-mobile-real-device-checklist.md`（RD-01 至 RD-26），对象是 live 展示入口 `https://test.nwm.ac.cn`，部署 commit `9c2724a18472097b011fbe94c4940789b8f3a8d7`（部署记录见 `docs/runbooks/receipts/2026-10-10-display-mobile.md`）。

## 结果来源

2026-10-10 项目 owner 在会话中声明“真机确认通过”，并要求据此收尾归档。本 receipt 按该声明记录：

- owner 给出的是**整体结论**，没有逐项回报，也没有提供设备型号、系统版本与浏览器版本；下表每一格的“通过”都来自这一句整体声明，不是逐项实测记录。
- 编排者没有接触真机，无法独立核实任何一项。
- RD-21 与 RD-26 清单要求在备注里写实际命中次数（n/5），未提供。

## 结果表

| 检查项 | iOS Safari 结果 | Android Chrome 结果 | 备注 | 后续 issue |
|---|---|---|---|---|
| RD-01 | 通过 | 通过 | owner 整体声明 | 无 |
| RD-02 | 通过 | 通过 | owner 整体声明 | 无 |
| RD-03 | 通过 | 通过 | owner 整体声明 | 无 |
| RD-04 | 通过 | 通过 | owner 整体声明 | 无 |
| RD-05 | 通过 | 通过 | owner 整体声明 | 无 |
| RD-06 | 通过 | 通过 | owner 整体声明 | 无 |
| RD-07 | 通过 | 通过 | owner 整体声明 | 无 |
| RD-08 | 通过 | 通过 | owner 整体声明 | 无 |
| RD-09 | 通过 | 通过 | owner 整体声明 | 无 |
| RD-10 | 通过 | 通过 | owner 整体声明 | 无 |
| RD-11 | 通过 | 通过 | owner 整体声明 | 无 |
| RD-12 | 通过 | 通过 | owner 整体声明 | 无 |
| RD-13 | 通过 | 通过 | owner 整体声明 | 无 |
| RD-14 | 通过 | 通过 | owner 整体声明 | 无 |
| RD-15 | 通过 | 通过 | owner 整体声明 | 无 |
| RD-16 | 通过 | 通过 | owner 整体声明 | 无 |
| RD-17 | 通过 | 通过 | owner 整体声明 | 无 |
| RD-18 | 通过 | 通过 | owner 整体声明 | 无 |
| RD-19 | 通过 | 通过 | owner 整体声明 | 无 |
| RD-20 | 通过 | 通过 | owner 整体声明 | 无 |
| RD-21 | 通过 | 通过 | owner 整体声明 | 无 |
| RD-22 | 通过 | 通过 | owner 整体声明 | 无 |
| RD-23 | 通过 | 通过 | owner 整体声明 | 无 |
| RD-24 | 通过 | 通过 | owner 整体声明 | 无 |
| RD-25 | 通过 | 通过 | owner 整体声明 | 无 |
| RD-26 | 通过 | 通过 | owner 整体声明 | 无 |

## 清单范围外的额外观察

| 设备 | 检查项 | 结果 | 说明 | issue |
|---|---|---|---|---|
| Android 厂商自带浏览器（型号与版本未提供），竖屏 | RD-02 | 失败 | 标题栏上方有一条约 40 CSS px 的浅灰空带，带内没有状态栏图标。从代码推断：外壳根节点的 `safe-area-inset-top` 内边距被该浏览器报告的非零顶部安全区撑开（未在设备上实测安全区取值）。 | #2857，owner 决定不处理并已关闭；若 iOS Safari 或 Android Chrome 上复现再重开 |

该浏览器不在清单要求的两类设备内，所以这一行不计入上表。

## 开放问题

`design.md` Open Questions 第 1 条（全国缩放级别下手指能否点中河段）对应 RD-26，按 owner 的整体声明记为通过；没有命中次数，探针在无头浏览器里点不中的原因仍未查明。
