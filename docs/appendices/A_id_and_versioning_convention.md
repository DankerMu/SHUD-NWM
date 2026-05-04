# 附录 A. ID 与版本命名规范

版本：v0.1  
日期：2026-04-30

## 1. 原则

1. ID 稳定、可读、不可复用。
2. 版本变化通过新 ID 表达，不能覆盖旧 ID。
3. 业务查询允许使用 active 标志，但存储必须记录具体版本。
4. run_id 应包含 run_type、source、cycle、basin/model 关键信息。

## 2. 命名建议

| 对象 | 格式 | 示例 |
|---|---|---|
| basin_id | `{name}` | `yangtze` |
| basin_version_id | `{basin}_vYYYY_MM` | `yangtze_v2026_01` |
| river_network_version_id | `{basin}_rivnet_vNN` | `yangtze_rivnet_v12` |
| model_id | `{basin}_shud_vNN` | `yangtze_shud_v12` |
| forcing_version_id | `forc_{source}_{cycle}_{model}` | `forc_gfs_2026043000_yangtze_shud_v12` |
| state_id | `state_{model}_{valid}` | `state_yangtze_shud_v12_2026043000` |
| run_id | `{run_type}_{source}_{cycle}_{model}` | `fcst_gfs_2026043000_yangtze_shud_v12` |
| curve_id | `freq_{method}_{duration}_{model}_{segment}` | `freq_piii_1h_yangtze_shud_v12_riv0001` |

## 3. River segment ID

```text
{river_network_version_id}_riv_{zero_padded_index}
```

## 4. Crosswalk

流域或河网变化时，不重用旧河段 ID。建立 crosswalk：

```text
old_segment_id
new_segment_id
overlap_length_ratio
upstream_area_ratio
relation_type: same/split/merge/changed
confidence
```

## 5. Scenario ID

```text
analysis_true_field
forecast_gfs_deterministic
forecast_ifs_deterministic
forecast_best_available
hindcast_replay
```
