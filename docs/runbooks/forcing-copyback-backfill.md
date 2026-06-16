# Historical Forcing Copyback Backfill

最后更新：2026-06-16
适用范围：node-22 计算控制面，用于补拷历史已发布 q_down run 引用的 forcing package 到 shared object-store mirror。

## 结论

默认命令只做 dry-run，不写目标目录。只有显式加 `--apply` 才会把通过校验的
`forcing/<source>/<cycle>/<basin_version_id>/<model_id>/` 包复制到 `NHMS_OBJECT_STORE_COPYBACK_ROOT`。

该工具不修改数据库、不推进 `hydro` 或 `met` 状态；审计证据来自 stdout JSON 报告。

## 环境变量

在 node-22 checkout root 执行，先加载计算控制面环境：

```bash
cd /scratch/frd_muziyao/NWM
set -a
source infra/env/compute.host.env
set +a
```

必须具备：

```bash
DATABASE_URL=<writer-or-readable-production-dsn>
OBJECT_STORE_ROOT=/scratch/frd_muziyao/nhms-prod/object-store
NHMS_OBJECT_STORE_COPYBACK_ROOT=/ghdc/data/nwm/object-store
```

如果生产对象 URI 依赖前缀，也保留：

```bash
OBJECT_STORE_PREFIX=s3://nhms
```

`OBJECT_STORE_ROOT` 是历史 forcing package 的 staging/source root；
`NHMS_OBJECT_STORE_COPYBACK_ROOT` 是 22 写、27 只读消费的 shared object-store mirror。
两者不能互相嵌套，也不能配置成同一个目录。发布时的 exact-root skip 语义不适用于本工具；
backfill 的目标是修复 shared mirror，same-root 属于配置错误。

## Dry-Run

先运行 dry-run 并保存 JSON：

```bash
uv run python -m services.tile_publisher.forcing_copyback_backfill \
  > /scratch/frd_muziyao/nhms-prod/workspace/forcing-copyback-backfill-dry-run.json
```

dry-run 会扫描：

- `hydro.hydro_run.status IN ('parsed', 'frequency_done', 'published')`
- `hydro.river_timeseries.variable = 'q_down'`
- `met.forcing_version` 中 joined 的 `forcing_package_uri`、`checksum`、`lineage_json`

扫描只读，不写数据库；查询从符合状态的 `hydro.hydro_run` 出发，用 `EXISTS` 验证 q_down 覆盖，
避免为发现候选而 materialize 全量 q_down run 集合。历史包很多时 stdout JSON 仍可能很大，建议始终重定向保存。

报告重点看：

- `copyable_package_count`
- `already_present_checksum_consistent_count`
- `missing_source_count`
- `checksum_mismatch_count`
- `legacy_key_rejected_count`
- `failure_count`
- `failures[]`

`failures[]` 中的 `run_id`、`forcing_version_id`、`forcing_package_uri` 和 `reason`
用于人工处理。形如 `forcing/{forcing_version_id}/` 的 legacy key 不会自动猜测迁移目标。

## Apply

确认 dry-run 后再执行写入：

```bash
uv run python -m services.tile_publisher.forcing_copyback_backfill --apply \
  > /scratch/frd_muziyao/nhms-prod/workspace/forcing-copyback-backfill-apply.json
```

apply 只复制满足同一套 publish-time forcing 校验的包：normalized key、source/cycle/basin/model
identity、manifest SHA-256、lineage manifest checksum 和 source tree 都必须一致。

目标端如果已经存在且 `forcing_package.json` checksum 与 `met.forcing_version.checksum` 一致，
报告为 `already_present`，不会重复计为 copied。

## 重跑

dry-run 和 apply 都可以重跑。checksum 一致的目标包会稳定进入
`already_present_checksum_consistent_count`，缺源、checksum mismatch、legacy key 或 unsafe path
仍会保留为 failure/manual item。

重跑前不要手动修改 DB 状态。该工具没有 DB 写入路径，不能用来修正 `hydro.hydro_run.status` 或
`met.forcing_version` 元数据。

## 回滚边界

工具在单个 package copy 失败时会使用 publish-time copyback helper 的目标替换/回滚行为，避免留下部分包。

已成功报告为 copied 的包如果需要撤回，回滚是人工文件操作：根据 apply JSON 中 `packages[].object_key`
在 `NHMS_OBJECT_STORE_COPYBACK_ROOT` 下隔离、删除或恢复对应目录。优先先挪到同一文件系统的 quarantine
目录，确认下游不再读取后再删除，例如：

```bash
target=/ghdc/data/nwm/object-store/forcing/<source>/<cycle>/<basin_version_id>/<model_id>
quarantine=/ghdc/data/nwm/object-store/.manual-rollback-$(date -u +%Y%m%dT%H%M%SZ)-<model_id>
mv -- "$target" "$quarantine"
```

隔离前必须先确认该目录对应 apply 报告中的 copied package，且没有被后续生产重新发布。数据库行不会自动回滚，
因为本工具从不修改数据库。
