# 05. Slurm + HPC 作业调度设计

版本：v0.2  
日期：2026-05-06

## 1. 设计原则

1. Web/API 服务不直接运行 SHUD。
2. Slurm 是所有重计算任务的唯一入口。
3. 作业必须由 manifest 驱动，支持独立重跑。
4. 每个流域模型、预报源、起报周期可独立运行。
5. 作业失败必须可定位到阶段、流域、run_id、日志。

## 2. 作业类型

生产提交通过 Slurm gateway 的 `job_type` 渲染 `infra/sbatch` 模板；
权威映射以 `services/slurm_gateway/config.py` 的
`DEFAULT_JOB_TYPE_TEMPLATES` 为准。

| `job_type` | 模板 | 说明 |
|---|---|---|
| `convert_canonical` | `infra/sbatch/convert_canonical.sbatch` | 转换为 canonical meteorological product。 |
| `produce_forcing_array` | `infra/sbatch/produce_forcing_array.sbatch` | 对每个流域/模型生成 forcing package。 |
| `run_shud_analysis` | `infra/sbatch/run_shud_analysis.sbatch` | 运行 analysis。 |
| `run_shud_forecast_array` | `infra/sbatch/run_shud_forecast_array.sbatch` | 批量运行 forecast。 |
| `parse_output_array` | `infra/sbatch/parse_output_array.sbatch` | 批量解析 SHUD 输出。 |
| `compute_frequency_array` | `infra/sbatch/compute_frequency_array.sbatch` | 批量计算重现期。 |
| `publish_tiles` | `infra/sbatch/publish_tiles.sbatch` | 发布瓦片和前端图层索引。 |

## 3. Job array 策略

```text
job_type: run_shud_forecast_array
template: infra/sbatch/run_shud_forecast_array.sbatch
array: 0-29%8
manifest_index: manifests/run_index.txt
```

含义：30 个流域任务，最多 8 个并发。每个 array task 通过 `SLURM_ARRAY_TASK_ID` 读取对应流域和模型。

## 4. 依赖关系

```text
canonical_convert
  → forcing_array
  → shud_forecast_array
  → parse_array
  → flood_frequency_array
  → publish_tiles
```

示例提交顺序：

```text
convert_canonical           -> infra/sbatch/convert_canonical.sbatch
produce_forcing_array       -> infra/sbatch/produce_forcing_array.sbatch
run_shud_forecast_array     -> infra/sbatch/run_shud_forecast_array.sbatch
parse_output_array          -> infra/sbatch/parse_output_array.sbatch
compute_frequency_array     -> infra/sbatch/compute_frequency_array.sbatch
publish_tiles               -> infra/sbatch/publish_tiles.sbatch
```

## 5. Resource profile

```json
{
  "partition": "compute",
  "nodes": 1,
  "ntasks": 1,
  "cpus_per_task": 32,
  "memory_gb": 128,
  "walltime": "06:00:00",
  "max_concurrent": 4,
  "shud_threads": 32
}
```

## 6. Workspace 结构

```text
/work/nhms/run_workspace/{run_id}/
  ├── run_manifest.json
  ├── model/
  ├── forcing/
  ├── input/
  ├── output/
  ├── logs/
  ├── parsed/
  ├── status.json
  └── done.flag
```

## 7. 作业状态回写

HPC 作业有两种回写方式：

1. 作业结束后写 `status.json` 到对象存储，由控制平面轮询。
2. 作业使用受控 service token 调用内部 callback API。

建议 MVP 使用方式 1，降低网络和安全复杂度。

## 8. 幂等性

- 如果目标输出已存在且 checksum 匹配，跳过。
- 如果存在 `failed.flag`，允许 `--force` 清理后重跑。
- 如果存在 `done.flag`，默认不重跑。
- 同一 `run_id` 不允许两个 Slurm 作业同时写同一 output_uri。

## 9. 失败处理

| 失败类型 | 判断 | 动作 |
|---|---|---|
| 下载缺文件 | manifest 文件数不匹配 | 重试下载，超过上限标记 failed_download。 |
| 转换失败 | canonical 缺变量或单位异常 | 标记 failed_convert，写变量级错误。 |
| forcing 失败 | 缺站点文件或时间轴不连续 | 标记 failed_forcing。 |
| SHUD 非零退出 | exit code != 0 | 保存 stdout/stderr，标记 failed_run。 |
| SHUD 输出缺失 | `.rivqdown` 或 `.rivystage` 缺失 | 标记 failed_parse（error_code=OUTPUT_REQUIRED_FILE_MISSING）。 |
| 解析失败 | 输出格式不匹配 | 原始输出保留，解析作业可单独重跑。 |

## 10. SHUD 调用

```bash
./shud_omp \
  -p input/{project}.cfg \
  -c input/{project}.cfg.calib \
  -o output/ \
  -n ${SHUD_THREADS} \
  {project}
```

## 11. 安全

- Slurm 提交服务只允许固定命令模板，不接受任意 shell。
- manifest 通过 schema 校验。
- 作业用户与 Web 用户隔离。
- 生产数据目录只允许白名单路径。
- 日志脱敏，不写入访问密钥。
