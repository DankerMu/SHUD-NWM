# frontend-no-unused-gate

## Why

前端没有任何“未使用的局部 / 参数”门禁（#2863 的决策项 (1)）：#2848 / #2871 里清掉的未读 prop、未用变量都是人工发现的。owner 裁定（2026-10-11，issue 评论）：启用 `noUnusedLocals` + `noUnusedParameters`——零依赖、并入现有 `pnpm typecheck` / `check:types`、零新增 CI 耗时；存量先清零再开门，不带 baseline。knip / eslint 不启用。

## What Changes

- `apps/frontend/tsconfig.app.json` 与 `tsconfig.node-playwright.json` 打开 `noUnusedLocals`、`noUnusedParameters`。
- 清掉现存违规（2026-10-11 实测：`tsconfig.app.json` 35 条 / 25 个文件，`tsconfig.node-playwright.json` 21 条，大量重叠；TS6133 与 TS6196）：未用的局部、import、类型直接删；签名上必须保留的未用参数改 `_` 前缀。

不改运行时行为；不加依赖；不动 CI workflow（两个开关随现有 typecheck 步骤生效）。design.md 省略（compact）。

## Triage

```text
Issue type: tooling gate + dead-local cleanup
Fixture level: compact
Upstream suggested level: 未给（issue 把它列为待裁定项）
Blast radius: 前端两个 tsconfig；约 26 个文件里的未用声明（12 条在测试、约 10 条在 playwright.*.ts、约 14 条在 src 非测试代码）
Selected risk packs: Behavior regression（删“未用”声明不得删掉有副作用的调用或被运行时依赖的参数位）
Evidence floor: 两个 tsc 开门后 0 错；vitest / build；dist 改前改后比对并逐条解释；mocked 车道；有 JS 差异时 node-27 live 守卫
```

## Impact

- 受影响文件：两个 tsconfig；违规清单中的文件（以实现时 tsc 输出为准）。
- 受影响规格：`frontend-testing`。
