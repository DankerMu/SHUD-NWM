## Context

这是 #1895 breadth/round-ceiling 后的完整前置切片，不是新建替代 rollout。原实现已分别保存，C4 已移植至最新 master 基底；子 PR frozen-SHA review 必须独立完成。项目 profile 为 NHMS；上游原 issue 无建议 fixture tier，本切片 expanded/high 源于 CLI/schema、权限、异步状态和私有文件发布。

## Goals / Non-Goals

目标：一个可独立安装、测试和调用的 C4 接受链；真实 `/`、GFS/IFS `/ops` 读路径与无控制副作用证明；拒绝假身份、过期/不完整/不安全证据。
非目标：不实现 Python owners、C1–C3、cold census/installer/performance/timer；不部署、不访问 node-27、不产出或声称 live PASS；不改变 river-click 1+20/P95 或十二 lane aggregator。

## Decisions

独立 Playwright profile 而非借用 monitoring/river-click 入口，防止跳过 C4 特有双源 job/log 证据。需要五个显式环境输入（frontend origin、API origin、basin、segment、receipt path），禁止 mock API 与角色 override。
只读 viewer 例外放在 `/ops` gate，通过 runtime 的两个一致字段授权；config loading 最长等待 10s，到期拒绝，晚到有效 readonly config 可以恢复。其它权限路径不放宽。
网络 observer 依据 phase/strict identity 关联请求；2xx headers 不等于完成。body、DOM evaluate（含 quiet recheck）受同一 step/whole deadline；正常 abort 不误拒，required failure、source error、权限拒绝、任何 Slurm 或非 GET/HEAD 控制请求阻止 PASS。
复用 TS 私有 publisher，与 C4/river-click 各自 domain error guard 解耦；0700 euid-owned parent、0600 regular single-link file、no-follow/exclusive/link-first、fsync/readback，不能覆盖旧结果或清理非本次创建对象。C4 binder 校验五输入、秒粒度执行 bracket、POSIX identity facts 和闭集 semantics。端到端 exact SHA 由交付 frozen-SHA/G0 门负责，C4 字节摘要及 reviewed-SHA 由 #1895 C3 publisher/binder 绑定；这些强制义务不取消，也不虚构 C4 CLI 的 SHA/digest 参数。Python 纳秒 bracket 不属于此切片。
普通测试用隔离 VM 重新编译浏览器 evaluate 函数验证序列化依赖，不为 Vitest 引入 browser binary；真实 profile 仍跑 Chromium。

## Sketch seams under test

- `test:e2e:live-c4-display` shell/config/global setup → lane owner：显式配置、real-only、deadline 与 terminal receipt。
- C4 lane fake page + isolated evaluate → 同一 producer：双源 identity、loading/quiet、网络失败/完成、无控制观测。
- schema/examples + `buildC4PassEvidence`/validator + binder CLI/core：闭集、来源/时间/job/log/隐私一致，生产 builder 可供后续 C3 fixture 消费但无 Python 运行依赖。
- `RBACGate`/App 与 monitoring store：readonly viewer 成功、其它 runtime/路由拒绝、多 stage 合法 jobs。
- 共享 private publisher 与既有 river-click consumer：路径/权限/并发/no-clobber/FD/原生错误兼容。

## Risk packs

Selected: Public API / CLI / script entry（独立命令）；Config / project setup（五输入与默认测试排除）；File IO / path safety / overwrite（私有发布与读取）；Schema / columns / units / field names（闭集 receipt）；Auth / permissions / secrets（严格 readonly gate、脱敏）；Concurrency / shared state / ordering（phase、quiet、deadline）；Resource limits / large input / discovery（响应/JSON/时间界）；Legacy compatibility / examples（river-click、非 ops 路由）；Error handling / rollback / partial outputs（BLOCKED/FAIL、只清理 owned temp）；Release / packaging / dependency compatibility（typecheck/build、无 Chromium 单测）；Documentation / migration notes（独立使用合同、rollout 禁区）。
Domain selected: Published NHMS artifacts / display identity（GFS/IFS strict pins）；Run manifest / QC provenance（仅 receipt provenance，非 scheduler manifest）。
Domain not selected: Geospatial / CRS / basin geometry、Hydro-met time series / forcing windows、SHUD numerical runtime / conservation / NaN、PostGIS / TimescaleDB domain behavior、Slurm production lifecycle / mock-vs-real parity、External hydro-met providers / snapshot reproducibility：不改这些计算/存储/提供方实现；C4 仅观察既有 display identity 和阻止 Slurm 请求。

## Invariant Matrix

Governing invariant: 只有同一次有界真实只读浏览器运行完整证明 `/` 与 GFS/IFS strict `/ops`，且私有 receipt 的身份/时段/字节未变化时，接受链才能给出 PASS。
Source of truth: runtime 双字段、实际 request/response/DOM、source/basin/version/network/run/model/cycle/scenario/job/log identity、schema 1.0、C4 秒粒度 bracket/POSIX facts；交付/G0 的 frozen SHA 与 #1895 C3 接受链的 C4 sha256/reviewed-SHA。
Producers: `playwright.c4-display-lane.ts`、`playwright.c4-display-evidence-owner.ts`、`src/lib/c4DisplayEvidence/receipt.ts`。
Validators/preflight: `config.ts`、lane preflight、schema/semantic validator、`c4-receipt-binder-core.mjs`。
Storage/publish: `playwright.private-receipt-publication.ts`、C4 publisher；仅当前执行创建的临时文件可清理。
Public/downstream: C4 shell/config/spec、`RBACGate`/App、monitoring store；unchanged sibling 为 river-click publisher、其它 RBAC 路由与普通 Playwright discovery。
Failure/stale: loading 等待；source/permission/runtime/required response 错误阻止 PASS；超时、变化/旧 evidence、非法文件和 secret 均 fail-closed。
Regression rows: 合法双源完整运行→PASS；缺配置→BLOCKED；required failure/quiet 后异常/超时→非 PASS；C4 错误身份/额外字段/job/log/bracket/POSIX facts→拒绝；端到端 SHA/C4 digest mismatch→由 #1895 G0/C3 门拒绝（本前置项不声称已运行）；symlink/FIFO/权限/nlink/已有目标/交换→拒绝且无覆盖；有效 readonly viewer→ops 可达但无控制，其它路由/runtime→既有门；river-click 既有成功/原生 errno/FD→契约不变；empty browser cache→Vitest 通过而 live profile 不伪造执行。

## Risks / Trade-offs

能力 merge 不等于生产 ready：node-27 live oracle 必须留在 #1895，未完成前无部署声明。多层校验有重复约束，但跨字段关系只由 semantic/binder 强制，标准 JSON Schema 不假装能比较 sibling 值。

## Migration Plan

从现有 WIP 显式切片，保留 readiness 与保护路径；C4 前置 PR 基于 master，不能带入旧 readiness commit。完整 readiness 随后依赖已合并 C4。正式部署前后 live 证明由 #1895 维护窗口承担；本切片不改变生产服务。

## Not yet specified

无切片范围内待决产品问题；live 基线与生产验收不在本切片提前决定。
