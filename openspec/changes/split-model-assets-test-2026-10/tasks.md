## 约定

- 分支 `feat/issue-3046-split-model-assets-test`，从 master 切出；一个 PR，`Closes #3046`。
- 不需要 node-22 / node-27（纯前端测试搬运）。
- PR 合并后 `openspec archive` 本目录。

## 1. #3046 拆分 `modelAssets.test.ts`

Suggested fixture level: compact
Minimal mergeable slice: atomic

Risk packs:
- Legacy compatibility — selected：拆分前后收集用例集合与断言不变（evidence = 集合对比 + `expect(` 多重集合 + 逐段字节比对）。
- 其余 pack — not selected：生产代码零改动。

- [x] 1.1 共享部分（imports 之外的 `:30-109` helper/常量：`success`、`failure`、`UNSAFE_MODEL_ASSET_ERROR*`、`expectNoUnsafeModelAssetErrorText`、`BASINS_MODEL_ID`/`MANIFEST_URI`/`PACKAGE_URI`、`makeBasinsModel`、`resetStore`）移入不被收集的 `apps/frontend/src/stores/__tests__/modelAssetsFixtures.ts`（只加 `export` 与必要 import，先例 `apps/frontend/src/components/map/__tests__/m11StationForcingPopupFixtures.tsx:1-8`：`import { expect } from 'vitest'`、`type ModelAsset`/`useModelAssetsStore` 等；不得含 `vi.mock`、顶层 `vi.mocked(...)` 或 hooks；文件名不得匹配 vitest include glob `src/**/*.test.{ts,tsx}`）。
- [x] 1.2 `describe('useModelAssetsStore')` 内用例按 issue「解决思路·推荐」的主题分到 4–5 个 `modelAssets<Topic>.test.ts`（行段按 base 上实测为准；sanitizer 与 lifecycle 两组可合并）；每个文件各自声明 `vi.mock('@/api/client', ...)` 与原 `beforeEach`（字节一致），describe 名保持 `useModelAssetsStore`；用例体按原顺序搬运，不改任何断言；原文件删除（或保留为其中一个主题文件）。每个文件的 import 只保留实际用到的符号（`tsconfig.app.json` 开了 `noUnusedLocals`，builder 组不用 `client`/`success`/`failure` 等），禁止用 `void x`、`eslint-disable`、`@ts-expect-error` 绕过。
- [x] 1.3 `.large-file-guard.json` 不改（该路径从未豁免）。

Evidence Floor:
- 拆分前后 `cd apps/frontend && corepack pnpm exec vitest list src/stores/__tests__/modelAssets` 收集集合相等（路径写死，避免混入 `src/api/__tests__/modelAssets.test.ts`）：36 条 = 26 `it` + `it.each` 展开 (2+3+3+2)；「拆分前」快照在改动前于 master 上取；比较时去掉文件路径前缀，只比 `<describe> > <title>`。
- `expect(` 多重集合相等：拆分前 110，拆分后新文件 + fixtures 合计 110，排序后 diff 为空。
- 逐段字节比对：各新文件 describe body **去掉 `beforeEach` 块**后按序拼接 == 原文件 `:117-1056`（允许缺失分组间空行，PR 写明）；另验证每个文件的 `beforeEach` 块与原 `:112-115` 字节一致；fixtures 去掉 `export ` 与新增 import 头 == 原 helper 段。
- 每个新文件（含 fixtures）`wc -l` < 1000；`grep -c modelAssets .large-file-guard.json` = 0。
- `git diff --stat origin/master -- apps/frontend/src ':!apps/frontend/src/**/__tests__/**'` 为空。
- `cd apps/frontend && corepack pnpm typecheck && corepack pnpm lint && corepack pnpm test` 绿。
- 大文件 guard：所有改动 staged 后，把 `{"tool_input":{"command":"git commit -m x"},"cwd":"<repo>"}` 经 stdin 喂给 `.claude/hooks/large-file-guard/large-file-guard.sh`，exit 0（被删除的原文件 `git show :path` 为空按 0 行计，不会误拦）。
