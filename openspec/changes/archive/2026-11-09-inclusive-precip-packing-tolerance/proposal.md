# Proposal: IFS 降水量化容差的边界改为闭区间（#3109）

## Why

#3090 把 IFS `tp` 去累积的负值容差定为 `max(floor, s_prev + s_curr)`，但显著性判定写的是 `delta <= -tol`，边界本身被算作显著。

2026-11-09，node-22 追赶到 IFS cycle 2026102300 时，f012 有 81 个格点的 delta 恰好为 `-0.1220703125 mm`，等于 `tolerance_mm = 2 × 0.06103515625`。这一时次因此被标成 `warning_negative_precip`，forcing 把它当作不可用产品剔除，导致：
- 整条 IFS cycle 的 forcing 4 次尝试（264 个 job）全部失败；
- 66 个模型拿不到 forecast；
- node-27 上该 cycle 的入库卡住。

canonical 的取值不受影响：`max(0, delta)` 让这些格点无论如何都是 0。

## What Changes

- `workers/canonical_converter/converter.py` 的 IFS 降水去累积，在**至少一条记录的量化步长已知**时：
  - `-tol <= delta < 0` 记为 small（noise，`quality_flag` 不变）；
  - 只有 `delta < -tol` 才算 significant。
  - 边界写法为 `delta >= -tol * (1 + 1e-9)`：相对容差 1e-9，不加绝对容差。
- 步长未知时（容差即 env floor）以及 GFS 的两条路径，比较逻辑保持逐字节不变，延续 #3090 "no step known = today's behaviour" 的约定。
- 同步修改 `grib_packing.negative_noise_tolerance_mm` 和 converter 注释中关于 `delta <= -tolerance` 的描述。
- 测试：
  - 用生产实测值钉住"恰好落在边界 → `ok` + small anomaly 带审计字段"；
  - 边界向外的浮点微扰 `-tol*(1+1e-12)` 仍判 small；
  - −2.5 步和 −3 步仍判 `warning_negative_precip`；
  - 无步长时，边界上仍判 significant（行为不变）。

## Impact

- 只影响"步长已知 + 恰好落在容差边界"的 IFS `tp` 时次：`quality_flag` 从 `warning_negative_precip` 变为 `ok`，anomaly 从 significant 改为 small。
- 取值不变。已转换的 canonical 不会自动重转。
- 线上恢复：该 cycle 的 forcing 行已是 `permanently_failed`，自然 pass 不会再碰它们。做法是按 manual-recovery runbook 打 manual-retry marker，不挪 canonical；forcing 阶段失败的 marker 会从 convert 重跑整条链，于是会用新代码重转。
- 未解决的同类风险：−3 步及以上的事件仍会让整条 IFS cycle 失败。"把降水 `warning_*` 视为 forcing 可用"是策略变更，留给 owner 决定，见 #3109。
