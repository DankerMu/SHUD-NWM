**分册：当前结论与节点拓扑**

本页是当前生产值守手册的 §1-§2 分册（#1103 拆分，正文逐字保留）。
索引与全部分册入口见 [`../current-production-ops.md`](../current-production-ops.md)。

## 1. 当前结论

- node-27 是当前 active production service host：本机 PostgreSQL `:55432`、
  source download、systemd-driven ingest、display API 和前端公网入口都在 27。
- node-27 source download 由用户级 systemd timer
  `nhms-node27-download.timer` 驱动，调用
  `scripts/node27_download_once.sh`，自动选择 00/12 UTC 业务 cycle 并把 raw
  manifest 写入共享 NFS object-store。
- node-27 每 10 分钟通过用户级 systemd timer
  `nhms-node27-autopipe.timer` 调用
  `/home/nwm/NWM/scripts/node27_autopipe_cron.sh`，再运行
  `scripts/node27_autopipeline.py` 扫描 NFS object-store、注册/解析 run、
  入库并刷新 display coverage。生产配置用两个独立 run worker 并行处理，
  每个 worker 使用独立数据库事务；所有 run 收敛后只执行一次最终 display publish。
- node-27 display API 由 user systemd `nhms-display-api.service` 托管，
  `scripts/ops/start-display-api.sh` 负责安装权威 unit、平滑接管和 smoke check；
  当前监听 `127.0.0.1:8080`，默认 2 workers；公网入口是 `https://test.nwm.ac.cn`。
- node-22 是计算与 Slurm host：运行 Slurm Gateway、诊断 API、DB-free
  production scheduler timer、Slurm/SHUD wrapper，并向 NFS 写
  object-store/published 产物；node-22 不作为当前 NHMS 业务数据库 writer。
- 完整 forcing 包和 SHUD run 输出的共享真相源是
  `object-store/forcing/...` 与 `object-store/runs/...`；`published/`
  只放 display products、tiles、logs、manifests。
- node-22 看到共享数据面为 `/ghdc/data/nwm/...`；node-27 看到同一份 NFS
  数据为 `/home/ghdc/nwm/...`。

## 2. 节点和服务

| 面 | 位置 | 当前职责 | 关键入口 |
| --- | --- | --- | --- |
| node-27 DB | node-27 `127.0.0.1:55432/nhms` | active PostgreSQL/PostGIS/TimescaleDB | writer `DATABASE_URL` from node-27 ingest env; display uses readonly `display.env` only |
| node-27 download | node-27 `/home/nwm/NWM` | 自动下载 GFS/IFS 00/12 UTC raw source cycles 到共享 object-store | `infra/env/node27-download.env` -> `nhms-node27-download.timer` -> `scripts/node27_download_once.sh` |
| node-27 ingest | node-27 `/home/nwm/NWM` | 扫描 object-store runs、seed registry、register、parse、publish、refresh coverage | `infra/env/node27-ingest.env` -> `nhms-node27-autopipe.timer` -> `scripts/node27_autopipe_cron.sh` -> `scripts/node27_autopipeline.py` |
| node-27 display API | node-27 `127.0.0.1:8080` | display_readonly FastAPI, `/health`, `/api/v1/*`, frontend backend | `infra/systemd/nhms-display-api.service` -> `scripts/ops/start-display-api.sh` |
| node-27 public entry | `https://test.nwm.ac.cn` | nginx reverse proxy to local display API | `/etc/nginx/conf.d/test.nwm.ac.cn.conf` |
| node-22 compute | node-22 `/scratch/frd_muziyao/NWM` | Slurm Gateway、diagnostic API、DB-free scheduler、Slurm/SHUD compute wrapper | `nhms-compute-scheduler.timer`, `/scratch/frd_muziyao/NWM/.venv/bin/python -m services.slurm_gateway`, Slurm jobs |
| Shared NFS data | 22 `/ghdc/data/nwm`, 27 `/home/ghdc/nwm` | object-store mirror, published artifacts, Basins source data | NFS mount, no rsync step |

Node-22 historical PostgreSQL `:55433` was archived and stopped on 2026-06-29
and is retained only as an explicit rollback archive. Do not use node-22 local
PostgreSQL as current NHMS production state. Current database checks and
ingest/write checks belong on node-27 against `:55432`.
