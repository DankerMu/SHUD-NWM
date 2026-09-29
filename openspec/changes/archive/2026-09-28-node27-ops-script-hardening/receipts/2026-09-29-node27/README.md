# node-27 post-merge receipts (PR #2691, master `f2657ed4e`)

- `display-restart-2638.log`：由 `receipt-2638.sh` 生成，#2638 的 live receipt。
  - 做法：从另一个 git 仓库作 cwd 执行 `scripts/ops/start-display-api.sh`；同时在 `:8081` 起一个诱饵 uvicorn，它来自另一个 checkout（`/home/nwm/tmp/yd-NWM-decoy`），命令行形如 `.../.venv/bin/python -m uvicorn apps.api.main:app`。
  - 结果：`repo_root=/home/nwm/NWM`；`restart_rc=0`；诱饵 PID 前后都是 249132（`DECOY_SURVIVED=yes`）；`:8080 /health` 200；新 MainPID 的 `Umask: 0002`。
  - 诱饵事后已清理（`ss_8081=0`）。执行时 node-27 上没有其他 `:8081` 监听；yd-web 在 `127.0.0.1:8082`，没有受影响。
- `mvt-cache-retention-dry-run-2627.json`：部署后手动执行 `systemctl --user start nhms-node27-mvt-cache-retention.service` 产生的 receipt。
  - basemap 阶段 `mode=dry_run`，planned 0，failed 0：`/home/nwm/.cache/nhms/mvt/basemap/tianditu` 下 12069 个文件（93M）没有一个超过 30 天，也没有不可组写的文件。
  - `.pbf` lane 按既有生产配置照常执行（deleted 1047）。为控制体积，逐文件列表已省略，只保留计数。
  - `NODE27_MVT_CACHE_RETENTION_BASEMAP_DELETE` **尚未启用**，等用户确认。
