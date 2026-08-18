## Why

Issue #1412：e2e GFS fixture 的 payloads_by_url 与 adapter 云镜像后端漂移——
默认 backend 链先试 cloud mirror（idx+Range+cdo），fixture 只登记 NOMADS bundle URL，
`-m e2e` 两条主链路用例在下载阶段裸 KeyError 长期不可执行（#332 未偿部分）。

## What Changes

- fixture 的 GFSAdapterConfig 钉 `source_backends=(GFS_NOMADS_BACKEND,)`：
  mocked oracle 本就只覆盖 NOMADS bundle 车道（mirror 车道需真 cdo，mock 无法诚实执行），
  同时对 ambient `GFS_SOURCE_BACKENDS` env 封闭。
- `packages/common/test_netcdf4.py` 新增 `encode_test_netcdf4_bundle`：
  cloud-era manifest 是 bundle 形态（每 forecast hour 一个 entry 携带全部变量），
  fixture 按 `entry.metadata.bundle.variables` 编码多变量数据集。
- m1 canonical 计数断言修真：f000 不发布 apcp/dswrf（GFS_F000_UNAVAILABLE_VARIABLES），
  期望 = hours*7 − 2（当 0 ∈ hours）。

## Non-Goals

- adapter/converter 生产代码零改动；mirror 车道的 e2e 覆盖（需 cdo 实机，属 node-27 oracle 域）。

## Risk triage

- Fixture level: compact（test-infra + 共享测试 helper 扩展）。Repair intensity: low。
- Risk packs: test-evidence selected（复活两条主链路 e2e）；providers/snapshot not selected
  （不改 adapter 行为）；其余 not selected。

## Must preserve

- `encode_test_netcdf4` 单变量语义与全部既有调用方不变（bundle 版是新增函数）；
- 非 e2e 收集路径（NHMS_RUN_E2E 未设时 skip）不变。
