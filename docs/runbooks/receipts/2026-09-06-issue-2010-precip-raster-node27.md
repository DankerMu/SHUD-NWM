# node-27 receipt — issue #2010 I8 降水栅格服务（`/api/v1/precip/{source}/{cycle}/index` + `/{valid_time}.png`）

- 日期：2026-09-06（node-27 本机 `+08:00`；run 2 UTC `2026-09-06T04:18Z` 即本机 12:18；run 1 `02:58Z`）
- 分支：`feat/issue-2010-precip-raster-service` ・ PR #2094 ・ epic #2003 ・ OpenSpec change `display-v2-national-timeline-precip-overlay` group 5
- **读数与 SHA 的对应**：下文读数取自 **run 2 = `b2092981`**（round-1 修复 `4e29818a` + fixture 钉定 `b2092981` 之后的 head；终态 push 只再加本 receipt 与 docs，`git diff b2092981 <final> -- '*.py' '*.yaml' '*.ts'` 为空）。run 1（`3b80f46e`，修复前）读数归档在 `.workplans/issue-2010/phase8/node27-receipt-run1-3b80f46e.log`，两次差异见 §8。
- 节点：node-27（`210.77.77.27`）。本 receipt **不连 DB**（两条 precip 路由 DB-free；`display.env` 只为拿到 `NHMS_SERVICE_ROLE=display_readonly` 等角色配置）。
- 执行方式：**未动生产**。生产 :8080（`/home/nwm/NWM` 在 `hotfix/node27-rollback-pre-2073` = `5a86841c`，porcelain 11 条本地未提交内容全程未碰）不 `git pull`、不重启；
  只做 `git fetch origin <branch>`（仅更新 remote-tracking ref）+ throwaway worktree `/home/nwm/tmp/wt-2094`（`git worktree add --detach FETCH_HEAD`），
  从中另起单 worker uvicorn `127.0.0.1:8090`：`set -a; . infra/env/display.env; set +a` + `NHMS_PRECIP_MIRROR_ROOT=/home/ghdc/nwm/object-store`
  + 专用空 `NHMS_MVT_FILE_CACHE_DIR=/home/nwm/tmp/precip-cache-2094` + `PYTHONPATH=/home/nwm/tmp/wt-2094` + 生产解释器 `/home/nwm/NWM/.venv/bin/python`
  （runner 先断言 `git diff --quiet HEAD FETCH_HEAD -- pyproject.toml uv.lock` 为真才复用解释器，未触发任何 `uv sync`）。**未导出** `NHMS_OBJECT_STORE_COPYBACK_ROOT`。
  runner 全文 `node27-receipt-2010.sh` 与原始日志归档在 `.workplans/issue-2010/phase8/`（本地）。
- 判别器：`GET /api/v1/precip/gfs/2026-09-05T12:00:00Z/index` 在生产 :8080 为 **404**、在 :8090 为 **200**；`/api/v1/runtime/config` 报 `service_role=display_readonly`、`display_readonly=true`。

## 1. 镜像现状（NFS `/home/ghdc/nwm/object-store/canonical/`）

- gfs 与 IFS 各 27 个周期目录 `2026082312 … 2026090512`（每日 00/12Z；`2026090512` 由 #2016/#2034 的 `canonical_precip_mirror` hook 在 run 1 与 run 2 之间镜像落地），grid `gfs/grid/gfs_0p25`、`IFS/grid/ifs_0p25`。
- 最新周期 `2026090512`：gfs `prcp_rate_or_amount/` 56 个 `.nc`（f003–f168），IFS 53 个（f000–f144 每 3 h + f150/f156/f162/f168：IFS 产品 144 h 之后为 6 h 节奏，`IFS_DEFAULT_FORECAST_RESOLUTION_SEGMENTS = ((144,3),(360,6))`，实测 f147/f153/f159/f165 不存在，这是完整镜像而非缺片）。

## 2. index（`display_catalog_cached`，冷读）

| source | http | 耗时 | `valid_times` | first | last | image_size | bounds | palette_version |
|---|---|---|---|---|---|---|---|---|
| gfs | 200 | 0.0018 s | 57 | `2026-09-05T12:00:00Z` | `2026-09-12T12:00:00Z` | `[1316, 1219]` | `[63, 8, 145, 64]` | `cma24h6-c09056a8` |
| ifs | 200 | 0.0217 s | 49 | `2026-09-05T12:00:00Z` | `2026-09-11T12:00:00Z` | `[1316, 1219]` | `[63, 8, 145, 64]` | `cma24h6-c09056a8` |

- gfs 57 = lead 0…168h 全部可解析（lead 0 跨周期取 `2026090500` f003–f012 + `2026090412` f003–f012，无 f000 不报缺）；ifs 49 = 0…144h（IFS 6 h 节奏下 f147/f153/f159/f165 不存在，lead 147h 起每个 24h 窗口都需要一个不存在的 3 h lead，index 按 spec「窗口完整才列出」止于 `cycle+144h`；已在 tasks.md Non-goals 记为已知限制，前端最后 24 h 隐藏降水层）——index 只列窗口完整的时次，与 PNG 路径判据一致。
- legend 两源一致：`(0.1,10,#A6F28F,"0.1–10") (10,25,#3DBA3D,"10–25") (25,50,#61B8FF,"25–50") (50,100,#0000FF,"50–100") (100,250,#FA00FA,"100–250") (250,null,#800040,"≥250")`，与 `/api/v1/layers` `precip` 条目的 `legend` 同一列表。

## 3. PNG（冷 / 热 / 304 / 拼写）

| source | valid_time | 冷 http / 耗时 / bytes | `file` | 热 http / 耗时 / `X-Tile-Cache` | `If-None-Match` |
|---|---|---|---|---|---|
| gfs | `2026-09-05T12:00:00Z`（lead 0） | 200 / 0.385 s / 45590 | `PNG image data, 1316 x 1219, 8-bit colormap, non-interlaced` | 200 / 0.0041 s / hit | **304**，bytes=0，同 ETag + `Cache-Control` |
| gfs | `2026-09-06T12:00:00Z`（lead 24h） | 200 / 0.221 s / 44296 | 同上 | 200 / 0.0050 s / hit | 304 |
| ifs | `2026-09-05T12:00:00Z` | 200 / 0.205 s / 51335 | 同上 | 200 / 0.0039 s / hit | 304 |
| ifs | `2026-09-06T12:00:00Z` | 200 / 0.223 s / 52776 | 同上 | 200 / 0.0061 s / hit | 304 |

- 冷响应头：`Cache-Control: public, max-age=300`、`ETag: W/"precip-<sha256>"`、`X-Tile-Cache: miss`、`Content-Type: image/png`；四张 ETag 两两不同。
- 拼写等价：`gfs/2026-09-05T12:00:00.000Z.png` → 200、`X-Tile-Cache: hit`、ETag 与 `Z` 拼写完全相同，缓存目录未新增文件。
- PNG 结构（gfs lead 24h）：`w=1316 h=1219 bitdepth=8 colortype=3 plte_entries=7`，IDAT 解压 `1605423 = 1219 × (1+1316)`，像素类别直方图 `{0: 545138, 1: 895718, 2: 131281, 3: 22952, 4: 7049, 5: 1930, 6: 136}`（含 136 个 ≥250 mm 像素）。

## 4. 错误形状

- 未镜像周期 `gfs/2026-08-20T00:00:00Z/index` → 404 `PRECIP_CYCLE_NOT_MIRRORED`，`details={"reason":"cycle_not_mirrored","source":"gfs","cycle":"2026-08-20T00:00:00Z"}`，无绝对路径。
- `ERA5/…/index` → 422（枚举前置，uvicorn 日志无任何 warning，即未到达文件系统）。
- 最老周期 lead 0：`gfs/2026-08-23T12:00:00Z/2026-08-23T12:00:00Z.png` → 404 `PRECIP_WINDOW_INCOMPLETE`，`details={"reason":"no_mirrored_cycle_before_window_end","window_end":"2026-08-22T15:00:00Z"}`（= C−21h，决策 11）。

## 4a. round-1 review 整点/视野门（决策 12，仅 run 2）

- `gfs/2026-09-05T12:00:00Z/2026-09-05T12:30:00Z.png` → **422** `VALIDATION_ERROR`（`"Precipitation instants must fall on a whole hour."`，`details.valid_time` 回显请求值）。
- `gfs/2026-09-05T12:30:00Z/index` → 422；`valid_time = 2026-09-01T00:00:00Z`（早于周期）→ 422；`valid_time = 0001-01-01T00:00:00Z` → 422（run 1 时该 URL 为裸 500，见 review cand-D1）。
- 四个探针之后缓存目录仍是 4 个文件、0 个 `.tmp`：门在任何文件系统写入之前生效。

## 5. 目录与兄弟面

- `/api/v1/layers` ids = `['discharge', 'river-network', 'met-stations', 'precip']`；`precip` 条目 `tile_format=png`、`image_url_template=/api/v1/precip/{source}/{cycle}/{valid_time}.png`、`index_url_template=/api/v1/precip/{source}/{cycle}/index`、`bounds=[63,8,145,64]`、`bounds_crs=EPSG:4326`。
- `/api/v1/layers/precip/valid-times` → 200 `{"valid_times":[],"items":[],"limit":100,"observed_count":0,"truncated":false}`（既有 non-discharge 分支）。

## 6. 缓存与容量

```
/home/nwm/tmp/precip-cache-2094/precip/gfs/2026090512/2026-09-05T12:00:00Z.cma24h6-c09056a8.a03e6c4101ec.png
/home/nwm/tmp/precip-cache-2094/precip/gfs/2026090512/2026-09-06T12:00:00Z.cma24h6-c09056a8.1f213e4893cf.png
/home/nwm/tmp/precip-cache-2094/precip/IFS/2026090512/2026-09-05T12:00:00Z.cma24h6-c09056a8.b2d1e7d64a22.png
/home/nwm/tmp/precip-cache-2094/precip/IFS/2026090512/2026-09-06T12:00:00Z.cma24h6-c09056a8.9329a34b773c.png
tmp leftovers: 0
```

- 目录对 `<S>/<K>` 与镜像 `canonical/<S>/<K>` 逐字节一致（`IFS` 大写），文件名含 `<palette_version>.<slice_digest>`。
- `df -h`：`/` 98G 用 72G（77%）；`/home` 1.7T 用 1.1T（70%，可用 474G）。四张 PNG 合计 194 KB（zlib 6 比 run 1 的 level 9 大约 +25%，冷生成快 2–5×）。

## 7. 清理

- :8090 uvicorn 已 kill（`pgrep -f "port 8090"` 为 0）；专用缓存目录已删除；`git worktree remove --force /home/nwm/tmp/wt-2094` + `prune` 完成。
- 生产 :8080 进程未动（pid 未变），`/home/nwm/NWM` HEAD 仍 `5a86841c`、porcelain 仍 11。

## 8. run 1（`3b80f46e`）与 run 2（`b2092981`）差异

| 项 | run 1 | run 2 |
|---|---|---|
| 最新周期 | `2026090500` | `2026090512`（#2016 hook 新落地） |
| 冷生成（4 张） | 0.57–1.16 s | 0.21–0.38 s（`_ZLIB_LEVEL` 9→6） |
| PNG 字节 | 34–42 KB | 44–53 KB |
| 整点/视野门探针 | 未实现（`:30` 会返 200 并写新缓存文件；`0001-01-01` 为 500） | 全部 422、无新文件 |
| 其余读数（index 57/49、1316×1219、hit/304、错误形状、目录条目） | 同 | 同 |

