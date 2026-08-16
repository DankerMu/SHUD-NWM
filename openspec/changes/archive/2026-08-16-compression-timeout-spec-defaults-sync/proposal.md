# Proposal: compression-timeout-spec-defaults-sync

## Why

`openspec/specs/hypertable-compression/spec.md` 的 "Compression runner timeout budget chain" requirement 仍声明默认三元组 840000 ms / 900 s / 940 s，与运行时真值不符：#1352（commit `192d194c`，PR #1353）按实测 ~6.0 s/GB 稳态 chunk 体量把默认改为 3600000 ms / 3900 s / 3940 s，但该提交只动了 `scripts/`、`infra/`、runbook 与测试，**未回写 capability spec**。规格是 spec-driven 流程的权威面：按 spec 推导默认值的人会拿到一套连单个稳态 chunk 都压不完的旧预算——正是 #1352 修掉的故障形状。（issue #1386；无运行时影响，纯文档权威失真。）

## What Changes

- MODIFIED requirement：默认三元组改述为 3600000 ms / 3900 s / 3940 s，并把 "byte-identical to the previously hardcoded values" 这句过时措辞改为直述现行默认 + 标注 #1352 标定来源与旧值（former）。
- 两个 scenario 数值同步（`defaults unchanged` 的 THEN 三元组；`wrapper wall guard is fail-closed` 的 wrapper 缺省 3900 s）。
- 预算链两腿不变式语义不变（3600+300=3900 leg 1 有余量、3900+40=3940 leg 2 恰等）。

## Capability 影响

- `hypertable-compression`：MODIFIED — 仅默认值声明与措辞，无行为变更。

## 非目标

- 任何 `scripts/`、`infra/`、`tests/` 改动。
- 归档目录 `openspec/changes/archive/2026-08-10-node27-compression-timeout-walls/` 不回改（历史事实快照）。
