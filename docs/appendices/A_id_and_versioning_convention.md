# 附录 A. ID 与版本命名规范

版本：v0.2  
日期：2026-05-06

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

### 3.1 PR-2 reach-level convention（现行 basins-registry-import）

PR-2 (issue #561) 以来，basins-registry-import 写入 `core.river_segment` 的 reach
行使用 reach-level 命名，对应 `gis/river.shp` 的 `Index` 字段（zero-padded 6 位）：

```text
{model_id}_reach_{iRiv:06d}
```

示例：`basins_qhh_shud_reach_000001`。`core.river_segment_crosswalk.external_id`
保留 segment 粒度，按 `gis/seg.shp` 的 `(iRiv, iEle)` 写为：

```text
{iRiv}:{iEle}
```

> 旧 `{model_id}_seg_{segment_order}_ord_{iRiv}_rec_{iEle}` 命名（PR #534 时代）
> 已被 PR-2 取代；`_delete_legacy_seg_rows` 在 reingest 时同事务内清除残余旧行，
> 避免 FK 孤儿。前端 segment 级 hover/popup 通过 Path C
> (`ST_LineSubstring`) 的 API 切片返回 `{model_id}_seg_{iRiv}_{iEle}` 形式衍生
> ID，与 reach 行 ID 解耦。

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
