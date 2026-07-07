# 附录 B. Run Manifest Schema 草案

版本：v0.2  
日期：2026-05-06

## 1. 用途

Run manifest 是 HPC 作业的唯一输入契约。作业读取 manifest 后即可独立运行，不依赖 Web 服务即时响应。

## 2. Forecast run manifest 示例

```json
{
  "schema_version": "1.0",
  "run_id": "fcst_gfs_2026043000_yangtze_shud_v12",
  "run_type": "forecast",
  "scenario_id": "forecast_gfs_deterministic",
  "source_id": "GFS",
  "cycle_time": "2026-04-30T00:00:00Z",
  "issue_time": "2026-04-30T04:00:00Z",
  "start_time": "2026-04-30T00:00:00Z",
  "end_time": "2026-05-07T00:00:00Z",
  "model": {
    "model_id": "yangtze_shud_v12",
    "basin_version_id": "yangtze_v2026_01",
    "model_package_uri": "s3://nhms/models/yangtze_shud_v12/model_package.tar.gz",
    "project_name": "yangtze_v12"
  },
  "initial_state": {
    "state_id": "state_yangtze_shud_v12_2026043000",
    "ic_file_uri": "s3://nhms/states/yangtze_shud_v12/2026043000/yangtze_v12.cfg.ic"
  },
  "forcing": {
    "forcing_version_id": "forc_gfs_2026043000_yangtze_shud_v12",
    "forcing_uri": "s3://nhms/forcing/gfs/2026043000/yangtze_shud_v12/"
  },
  "runtime": {
    "executable": "shud",
    "threads": 32,
    "init_mode": 3,
    "ascii_output": 1,
    "binary_output": 0
  },
  "outputs": {
    "output_uri": "s3://nhms/runs/fcst_gfs_2026043000_yangtze_shud_v12/output/",
    "log_uri": "s3://nhms/runs/fcst_gfs_2026043000_yangtze_shud_v12/logs/",
    "required_files": ["*.rivqdown.csv", "*.rivystage.csv"]
  }
}
```

## 3. 必填字段

```text
schema_version
run_id
run_type
scenario_id
start_time
end_time
model.model_id
model.model_package_uri
forcing.forcing_uri
runtime.executable
outputs.output_uri
```

Forecast run 必须额外包含：`source_id`、`cycle_time`、`initial_state.ic_file_uri`。

## 4. 校验规则

- `end_time > start_time`。
- `run_type in [analysis, forecast, hindcast]`。
- `runtime.init_mode=3` 时必须存在 `initial_state.ic_file_uri`。
- `output_uri` 不允许与已有 published run 相同。
- 所有 URI 必须位于允许的 prefix 内。
