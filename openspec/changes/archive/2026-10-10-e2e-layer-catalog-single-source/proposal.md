# e2e-layer-catalog-single-source

## Why

图层目录 mock 字面量在 mocked 车道的支撑文件与 spec 间重复十余处（#2879，拆自 #2868 第 (5) 项）：`Schemas['Layer']` 或目录语义一变要同步改十几处，漏改的那份会让对应 spec 测一个产品已经不会收到的目录。

## What Changes

只动 `apps/frontend/e2e/**`：

- 新建 `e2e/support/layerCatalog.mocked.ts` 作为单一来源：最小径流条目、带额外 metadata 的径流条目（第二种形状，原样保留）、降水条目、`MOCK_LAYERS`、`MOCK_PRECIP_LEGEND`（由 `legendLauncher.mocked.ts` 搬入并转导出）、“径流目录 + 其余 `/api/v1/**` 回 `[]`”的宽路由安装函数。
- 响应等价的那一组 spec / 支撑文件改用共享安装函数；路由处理器不同的那一组只 import 条目常量、保留各自的处理器。

不改任何断言、期望值、产品代码；`e2e/m15-visual-conformance.spec.ts` 保持自包含；`riverWindow.mocked.ts` 自己的径流条目（带瓦片模板与有效时刻）不在范围内。design.md 省略（compact）。

## Triage

```text
Issue type: refactor (test support)
Fixture level: compact
Upstream suggested level: compact (agree)
Blast radius: mocked 合并门里十余个 spec 的输入；改坏时某个 spec 收到与原来不等价的目录或兜底响应而不自知
Selected risk packs: Legacy compatibility（每个 pathname 的响应体 / 状态码 / content-type 逐字段等价）
Evidence floor: 盘点表（含组 1 逐调用点单路由确认）+ 字面量与响应信封的字节比对；`--list` 测试集合不变；完整 mocked 车道；治理 hard gate；mobileSpecOracles 常量未改
```

## Impact

- 受影响文件：`apps/frontend/e2e/support/{layerCatalog(新),zoomControl,siteHeader,legendLauncher,opsEntry,controlBar}.mocked.ts` 与内联该字面量的 mocked spec（以实现时 `git grep` 为准）。
- 受影响规格：`mobile-regression-evidence`。
