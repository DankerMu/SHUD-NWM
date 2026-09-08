# C4 独立切片接线报告（#2123）— 2026-09-07

范围：已实现 C4 切片终态接线 / 使用文档 / 安全 red-proof；不重写能力，不跑完整 frontend pipeline。

## 依赖与 master 重叠

cherry-pick `--no-commit` 源 `0ed538ba`，base `e6b5e4ff`。自动合并无冲突。`scripts/select_ci_tests.py` 不在本切片。readiness 仍在 `2f7e95b4`，未混入。

| 重叠文件 | 保留的 master 行为 | 本切片增量 | 覆盖？ |
|---|---|---|---|
| `apps/frontend/package.json` | 既有 scripts（含 `test:e2e:live-display` / `live-river-click`） | 只加 `test:e2e:live-c4-display` | 否 |
| `.github/workflows/ci.yml` | `frontend:` 仍含 `apps/frontend/**`、`openapi/**`；`schemas:` 仍为 `schemas/**` | 只给 C4 schema/examples 加 frontend 路由，触发 AJV negatives | 否 |
| `tests/test_select_ci_tests.py` | `_database_filter_block` / `_backend_filter_block` 及全部既有断言 | 新增 `_frontend_filter_block` + `test_c4_schema_only_change_runs_frontend_ajv_negative_suite` | 否 |
| `playwright.config.ts` | 既有 `testIgnore` 项 | 追加 `live-c4-display.spec.ts` | 否 |
| `playwright.config.helpers.ts` | live-display / river-click helper 仍在 | C4 matcher + 任意 route/HAR 静态拒绝；readonly helper 改为双字段 AND | 否（AND 是 delta 要求，见下） |
| `playwright.river-click-evidence.ts` | `RiverClickPublicationError`、basename、no-clobber/FD 契约 | POSIX 抽到共享 primitive，wrapper 错误类不变 | 否 |
| `src/__tests__/riverClickPublisherFdLeak.test.ts` | FD / 原生 errno / 无 temp | 断言改为原生 EBADF → `PARENT_CHANGED`，契约同向加强 | 否 |
| `src/stores/monitoring.ts` | `validateRuntimeConfig` 对 `display_readonly` 角色仍强制 flag | `isDisplayReadonlyRuntimeConfig` 从 OR 改为 AND | 否（delta 双字段；旧 OR 会把 `compute_control`+flag 误判只读） |
| `src/App.tsx` | `/monitoring`、`/system/model-assets` 原角色门 | `/ops` 增加 `allowDisplayReadonly` | 否 |
| `RBACGate.tsx` + 测试 | operator/model_admin/sys_admin 放行；viewer/analyst 拒绝 | 10s 有界等待、双字段 viewer 例外、冲突拒绝 | 否 |
| `tsconfig.node-playwright.json` | 既有 river/live-display include | 追加 C4 模块 | 否 |

## 规格接线

base `ops-display-downgrade` / `single-map-shell-routing` 在切片里仍只写单字段 `display_readonly`/`service_role`，与已批准 delta（双字段 + 10s）不一致。已把这两条 **既有 requirement** 对齐到 delta 措辞；未改其它 requirements（导航降级、meteorology 合同、旧路由 redirect 等）。

## 使用文档

`openspec/changes/c4-live-display-readiness/README.md`：五输入、私有路径、real profile、binder、退出矩阵、生产禁区。未改 cold readiness runbook。

## 安全 red-proof

禁止 stash/reset/checkout/clean，禁止改活动 source。隔离目录：`artifacts/c4-red-proof-2123/`（gitignore `/artifacts/`）。同一 shipping tests 对隔离副本跑；vitest 配置只存在于该 ignored 目录。未跑完整 `pnpm test` / tsc / build。

| mutant | 隔离改动 | shipping test | 结果 |
|---|---|---|---|
| baseline | 无 | config / publisher / binder / RBACGate | 绿（6 / 14 / 5 / 15） |
| role-override | 删除 `VITE_AUTH_ROLE`/`VITE_ENABLE_ROLE_OVERRIDE` 拒绝 | `src/lib/c4DisplayEvidence/__tests__/config.test.ts` | 红：`rejects VITE_AUTH_ROLE… expected true to be false` |
| clobber | 跳过 `TARGET_EXISTS` 并在 EEXIST 时 unlink+link | `src/__tests__/c4DisplayPublisher.test.ts` | 红：`expected a C4PublicationError`；`expected '' to contain 'TARGET_EXISTS'` |
| binder-jobs | 删除 GFS/IFS `job_id` 不等约束 | `src/__tests__/c4ReceiptBinderCore.test.ts` | 红：`equal source job ids: expected true to be false` |
| dual-field | `isDisplayReadonlyRuntimeConfig` 退回 OR | `src/components/layout/__tests__/RBACGate.test.tsx` | 红：4 条冲突 runtime 找不到「权限不足」 |

活动 source 无 `MUTANT:`；未动共享 stash。副本在证明后删除，只清本次对象。

## 未改动的 must-preserve

river-click 格式/错误类/FD/no-clobber；非 ops RBAC；role override 禁用；body/deadline/quiet；schema/semantic/binder 闭链。C4 binder 绑定五输入 + 秒级 bracket + POSIX identity facts；git frozen-SHA 仍属 #1895 Evidence Floor，不是本 CLI `--sha`。
