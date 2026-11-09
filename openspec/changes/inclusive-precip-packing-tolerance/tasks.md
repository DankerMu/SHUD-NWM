# Tasks: inclusive-precip-packing-tolerance (#3109)

Fixture level: minimal
Risk packs: data-quality-classification（只改 quality_flag，不改取值）、canonical-lineage（受影响时次的 lineage/checksum 会变）
Must preserve: 无步长路径和 GFS 路径的 anomaly dict 与 flag 逐字节不变；`max(0, delta)` 取值不变；#3090 的现有测试全部保持原断言。

## 1. 实现
- [x] 1.1 IFS 降水去累积（`converter.py`，`_tol = negative_noise_tolerance_mm(...)` 附近）：`step_known` 时边界闭合，`-tol <= delta < 0` 记为 small，`delta < -tol` 记为 significant，边界用 `delta >= -_tol * (1 + 1e-9)` 判为 small（相对容差 1e-9，不加绝对容差）；`step_known` 为假时比较不变
- [x] 1.2 `grib_packing.negative_noise_tolerance_mm` docstring 和 converter 中文注释同步说明边界语义
- [x] 1.3 测试（`tests/test_ifs_canonical.py`）：
  - 生产值边界用例：steps 均为 `2^-14` m（0.06103515625 mm），delta 为 −2 步，结果为 `ok`，anomaly 是 small 并带 `tolerance_mm=0.1220703125` 和步长；
  - 浮点微扰用例：直接调用分类逻辑，delta 取 `-tol*(1+1e-12)`（向外扰动），仍判 small；
  - −2.5 步（步长同为 `2^-14`）判 `warning_negative_precip`，用于拦住过宽的容差；
  - −3 步（同为 `2^-14`）仍判 `warning_negative_precip`；
  - 无步长时 delta 恰为 −0.1（等于 floor）仍判 significant。

## 2. 上线与恢复（合并后）
- [ ] 2.1 node-22 ff-only pull（禁止 `uv sync` 和裸 `uv run`）
- [ ] 2.2 实测前置状态（2026-11-09）：
  - forcing 行（两个 cohort master：`cycle_ifs_2026102300_convert_dg_8a34ed2ba8f8dd22f2716405569628a9`、`cycle_ifs_2026102300_convert_cohort_71f624007af5`）已是 `permanently_failed`、`retry_count=3`，自然 pass 不会再碰它们；
  - raw `raw/IFS/2026102300/` 仍在（53 个 grib，带 manifest）。

  按 `docs/runbooks/node22-control-plane-manual-recovery.md` 的 "permanent_failure" 一节处理。失败在 forcing 阶段时，marker 会从 convert 重跑整条链。步骤：
  - (a) **不 mv canonical**：marker 会从 convert 整链重跑，converter 发现 checksum 变化后会原子重写 f012 和 catalog（`converter.py:1943-1963`、`:1423-1457`）；mv 反而会把恢复引到要求 raw manifest 的零行分支，多一个阻塞点；
  - (b) `scripts/node22_manual_retry_failed_runs.py` 先不带 `--execute` 预览两个 run id，确认 restart 从 convert 开始，再加 `--execute`；下一个 pass 里，该候选的 `state_evidence` 应为 `manual_retry_requested`，且从 convert 重启，不是 blocked。
- [ ] 2.3 下一个 pass 重跑后：
  - catalog 中 f012 为 `ok`，anomaly 是 small 并带审计字段；
  - IFS forcing 成功（sacct）；
  - IFS forecast 完成；
  - node-27 上 10-23T00 达到 132 个产物并入库

## Evidence Floor
- `uv run pytest -q tests/test_ifs_canonical.py tests/test_grib_packing_reader.py tests/test_canonical_converter.py`，加上 selector 选出的测试集
- `uv run ruff check .`
- 2.3 的线上证据贴到 #3109
