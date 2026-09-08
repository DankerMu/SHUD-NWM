## Why

#1895 的 rollout readiness 逐步包含了独立 C4 浏览器证据能力，已触发 review round ceiling。按 `compressed-chunk-cold-tablespace-tiering/evidence/issue-1895-readiness-review-retro-20260907.md` 的 split 纠正动作，先交付完整 C4 能力，再交付 Python/readiness，避免继续审查整个大工作树。

## What Changes

- 增加独立 `test:e2e:live-c4-display`，真实访问 `/` 和 GFS/IFS strict `/ops`，产生私有、闭集、可绑定的 C4 receipt。
- producer、schema、semantic validator、private publisher、CLI binder 与负向测试同批交付；复用既有 river-click 的公共机制而不改变其 receipt 契约。
- `/ops` 仅在 runtime 同时报告 `service_role=display_readonly` 与 `display_readonly=true` 时允许 viewer 只读访问；其它路由、控制权限和生产角色来源保持不变。
- 普通 Vitest 不依赖 Chromium；live profile 仍要求真实 Chromium 和显式 production 配置，禁止 mock/live 混淆。

## Capabilities

### New Capabilities
- `c4-live-display-evidence`: 独立的 C4 browser producer、私有发布、schema/semantic/binder 证据链。

### Modified Capabilities
- `ops-display-downgrade`: 明确只读 runtime 下 viewer 的 `/ops` 例外，保持控制边界。
- `single-map-shell-routing`: 保留运维路由，限定 `/ops` 的 readonly viewer 访问条件。

## Impact

仅 frontend C4、共享 TS private publisher、readonly `/ops`、C4 schemas/examples、对应 CI 路径路由和规格。没有 Python readiness/DB/installer/timer 行为；不关闭 #1895/#1891。先合并代码能力不等于生产验收：完整 readiness 合并之前禁止 node-27 访问；生产部署与 C1–C4 live receipt 仍由 #1895 完成，共享 cold-tiering change 保持 active。

## Split contract

Implementation-ready 子 issue：#2123（#1895 前置，不关闭父 issue）。

Suggested fixture level: expanded。Repair intensity: high（权限、私有证据与异步 deadline）。
Minimal mergeable slice: atomic — 一个 C4 producer→validator→publisher→binder 的完整能力，readonly `/ops` 是该能力真实运行的必要前提；分开会留下不可执行的生产验证入口。
Width exception: multi-path - browser/文件系统/RBAC 单测是同一 C4 接受链的故障注入证据，不是独立业务交付；更细切片会分离证据生产与接受安全契约。

此次 fixture 是已有实现的事后拆分契约，不追溯声称实现前 fixture review 已执行，也不沿用旧工作树 clean review 为子 PR approval。
