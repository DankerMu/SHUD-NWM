# ADR 0007 — 业务化 SHUD 换用 OpenMP lineage 二进制

- 状态：Accepted
- 日期：2026-08-26

## 背景

业务化预报的 SHUD 二进制自建库以来一直是串行构建（SHUD commit `3aec657`）。
`DankerMu/SHUD-OpenMP` 的 `cpu-accel-v1.1.1`（SHUD commit `c7404df`）提供 OpenMP
加速。`c7404df` 是 `3aec657` 的严格 fast-forward 后代（`git merge-base` 相等，
`rev-list --left-right --count` 为 `0 135`），无分叉。

采纳前需回答一个问题：结果是不是 bitwise 的。

## 实测结论

在 node-22 上以 4 个流域（`Huai-MAIN` 15947 单元、`LH-GL` 23106、
`longmen_zhi_sanmenxia` 18949、`sanmenxia_zhi_huayuankou` 4704）逐格比对整个
`output/` 树（含续算状态 `.cfg.ic.update`，排除携带墙钟的 `.time.csv` / `.SHUD`）：

1. **跨线程逐字节不变。** `NUM_OPENMP ∈ {1,2,4,8,16}`、`OMP_NUM_THREADS` 与
   `SHUD_RHS_THREADS` 各种组合下，全树 SHA 与 `nFCall` 全等。
2. **与现网二进制不 bitwise。** 差异来自 135 个提交的 lineage 演进，与 OpenMP 无关
   （同 lineage 的串行构建 A 与并行构建 E 全等，而 A ≠ P）。水文量级：
   NSE 0.99949–0.99994、KGE 0.9899–0.9990、峰值比 0.9927–1.0079。
3. **加速真实。** 以现网二进制为分母：`Huai-MAIN` 3.75×@8 线程、`LH-GL` 3.11×@8。
   生产 4 核档实测 2.61×。
4. **输出文件集只增不减**：多产 `cvode_stats.txt`、`nfcall.txt` 两个诊断文件。

## 决策

换用 `cpu-accel-v1.1.1`（Config E），生产线程数取 `cpus_per_task`（当前 4）。

## 权衡

**接受非 bitwise。** 换二进制会改数。理由：差异源于上游 135 个提交的正常演进而非
并行化引入的不确定性，量级在 NSE ≥ 0.9995；而拒绝它意味着永久放弃 2.6× 加速并把
业务锁死在一个不再演进的 commit 上。

**放弃 Config E2**（`SHUD_NVEC_DETRED=1`，固定树规约，6.4–6.7×）。它自成一支
golden lineage，与 E 不 bitwise，等于再叠一层数值变更。先上 E，E2 留作后续独立评估。

**GEOL_KSATH=2.0 保留不动。** 加速买不到水文真实性：该参数存在 basin-specific 的
求解代价悬崖，从 2.0 挪向率定值 0.004 会让 f_eval 从 843 涨到 386,893（459×），
且悬崖以下非单调。2.6× 填不上这个量级。见 ADR 0005 与 `GEOL_KSATH` 相关 receipt。

## 一次性效应

切换那一刻，每个流域的下一个 cycle 消费的是**旧二进制写的** `.cfg.ic.update` 作为
热启动初值。数值上无害（两 lineage 的状态量在上述精度内一致），但这是预期内的过渡态，
不是 bug。

## 部署

二进制装在 `/scratch/frd_muziyao/shud-bin/cpu-accel-v1.1.1/shud`（带 `PROVENANCE.txt`
记录 commit、构建参数与 sha256），**不指向按日期命名的评估目录**。
`packages/common/shud_preflight.py` 的 `ldd` 预检会对缺库 blocker，无需额外把关。
