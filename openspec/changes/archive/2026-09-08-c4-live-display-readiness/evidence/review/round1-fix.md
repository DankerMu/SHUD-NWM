# PR #2126 round 1 fix

Source reviewed SHA: `775dfe7c9fe6652826fd2bb9e47ae24084a7a76f`。一个 implementer fix pass 处理全部 4 个 CONFIRMED findings；不重置轮次。

## Changes

- C4-R1-1：`c4ReceiptBinderCore.test.ts` 以公开 `acceptC4Receipt` 覆盖七个缺参、非十进制/逆序 bracket、mtime/started/ended 越界、receipt mode/nlink、parent mode、hook 驱动 parent identity 改变；另执行真实 CLI 缺 `cmd-end`。没有修改 binder 实现或新增 SHA/digest flags。
- C4-R1-2：`c4DisplayLane.test.tsx` 通过 shipping `runC4DisplayLane` 覆盖完整 200 矛盾 runtime body 与 response completion error，两者精确为 `RUNTIME_CONFIG_INVALID`。lane 与 fake harness 未改。
- C4-R1-3：仅将 spec/design 的粗略“任一缺配置→BLOCKED”改为既有有意分类：URL/receipt path 缺失→BLOCKED；basin/segment pin 缺失、空白或非法→FAIL CONFIG_INVALID。实现、README、schema 和既有 config test 未改。
- C4-R1-4：`validateRuntimeConfig` 不再把原始 `display_readonly:false` 改写为 true；仍强制 readonly role 的 control/slurm false 与 readonly queue mode。新增 mocked `client.GET` → shipping `fetchRuntimeConfig` → `RBACGate allowDisplayReadonly` 的 viewer deny proof，并保留 safe-control normalization assertions。现有 API 仍只产生一致双字段，不声称当前 live exploit。

## Implementer evidence

Focused green: changed five test files 87 tests PASS；app/tooling typecheck PASS。Isolated copy mutant: 15/82 failures when removing binder argument/bracket/window/POSIX/identity guards、改 lane failure code、恢复 store false→true coercion；shipping baseline green。Proof logs under ignored `artifacts/c4-pr2126-r1-fix-20260907/` and `apps/frontend/artifacts/c4-pr2126-r1-mutant/`；不声称这些 ignored logs 随 PR 发布。

The CLI missing-argument test executes shipping CLI and is not import-aliased into the mutant, so it does not independently contribute a mutant failure; in-process missing-argument cases do. This limit is explicit.

## Boundary and sibling checks

Binder/lane production hashes unchanged from reviewed head. `MonitoringPage` remains mutation-disabled by normalized controls；monitoring `fetchAll` skips queue by readonly role/mode；overview queue may request on contradictory data because its helper now rejects readonly, but current API cannot emit that pair and this is fail-closed rather than an auth bypass. Valid readonly, late config recovery, non-ops RBAC and river-click are unchanged.

Main orchestrator must run affected test/type/spec rows and record exact fix SHA. No backend pytest/collect, node-27/live, deployment or full frontend rerun was performed by implementer. Post-fix round uses maximum 3 seats, with at least one full-PR reviewer.
