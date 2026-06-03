# Issue #269 动态动作流 + 进度工作日志

## 1. 目标与边界
- Issue #269: GFS/IFS 降水率用静态 native_time_resolution 重建 → 边界帧错误
- 决策: **Option B 根因统一**（用户裁定）。GFS/IFS converter 用实际 step 在内部转 mm/day（镜像 ERA5），canonical unit mm→mm/day，producer 对三源统一透传 1.0，消除 producer 对 native_time_resolution 的降水依赖与整个 bug 类。
- Branch: feat/issue-269-precip-mmday-at-converter

## 关键证据
- ERA5 converter 已用实际 step 转 mm/day（converter.py:709 `delta*1000*24/step_hours`，unit mm/day），producer 透传——正确，无需改。
- GFS APCP（converter.py:645）返回原始 per-step mm（未除 step），unit "mm"。
- IFS（convert_ifs_precipitation_with_metadata, converter.py:809）返回 per-step mm（deltas_mm），已算 step_hours（:773），unit "mm"。
- 端到端最终幅值不变（IFS 仍 16.0：换算从 producer 移到 converter）。

## 状态机
| phase | 状态 |
|---|---|
| A 评估+决策 | ✅ Option B |
| B 修复 | ⏳ |
| C 远端验证 | ⬜ |
| D 评审 | ⬜ |
| E 验证门 | ⬜ |
| F 循环 | ⬜ |
| G merge | ⬜ |

## 进度日志
| 时间 | 阶段 | 动作 | 结果 |
|---|---|---|---|
| T0 | A | ground-truth #269 + 用户裁定 Option B | ✅ |
