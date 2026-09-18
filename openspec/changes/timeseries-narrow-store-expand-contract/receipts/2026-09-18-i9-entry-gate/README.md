# I9 / 6.1 开门检查收据（2026-09-18）

`tasks.md` 6.1 的只读一半。**结论：窗口内 legacy 路由 run = 0，门已开，未执行任何生产写。**

## 采集方式

node-27（active primary，`nhms-db` :55432），以

```
PGOPTIONS='-c default_transaction_read_only=on'
```

在会话层强制只读后运行 `scripts/node27_river_narrow_reparse_backfill.py plan`。DSN 经 `infra/env/node27-ingest.env` 由 env 传入，**未进入 argv、未落入本收据**。收据原始输出写 node-27 的 `/home/nwm/tmp`，仓库工作树未被触碰；node-27 当时在 `258b06ec`，**未 pull**（只读检查不应顺带部署代码到活跃主库）。

## 数字

| 指标 | 值 |
|---|---|
| 窗口内 legacy 路由 run | **0** |
| 窗口外 legacy 路由 run | 2919（published 2685 + superseded 234） |
| `candidates`（待 reparse） | 0 |
| `compressed_narrow_overlap` | `[]` |
| `artifact_missing` | 0 |
| `window_days` | 21 |

## 读法

- 21 天保留窗口已把全部 legacy 路由 run 熬出窗外，正是 runner docstring 预期的终局：窗口已过的 run 保持 `legacy`，其事实本就被保留期淘汰，随 contract 迁移与表一起 DROP。
- `candidates: 0` —— #2382 的 reparse 通道在生产上自建成起从未有可做之事，因此不存在“未归档的 reparse 收据”。本文件即 6.1 的归档物。
- 2919 与 `fixtures/I9-1988.md` C4 中冻结覆盖行的总数一致（两处独立取数）。这些行受 #1446 覆写守卫（`packages/common/display_coverage.py:699-701`）保护，保持冻结，不被清零或删除。

## 6.1 第三项门（no shape regression）—— 由既有收据结清，无需新测

该判据的定义原文（`fixtures/I8b-1987-live.md:135`）即“any pinned curve / MVT / display shape that regresses by an order of magnitude against the legacy baseline, or any Seq Scan on a fact table, **blocks the contract (I9)**”。

`../2026-09-18-i8-task52/` §10 已对其逐条判定：最差比 **2.19×**（远在一个数量级内）、**fact-table Seq Scan 为零**（`gate0918.py` 判了 58 个 fact plan 节点，每个触及 fact relation 的都是 Index Scan / Index Only Scan / 压缩块上子节点为索引扫描的 Custom Scan），结论逐字为 “**Criterion not tripped; it does not block I9.**”

其实测对象是活进程 `258b06ec`——本次只读检查时复核 node-27 仍在该 sha，故该证据对 I9 直接有效。

**故 6.1 三项门全部满足，全程零生产写。**

## 未覆盖

这 2919 行的 `refreshed_at` 新鲜度与残留 chunk 情况不在本次检查内，留待 6.2 迁移前。
