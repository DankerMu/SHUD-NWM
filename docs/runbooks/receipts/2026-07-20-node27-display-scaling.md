# node-27 全国展示扩展性能验收回执（2026-07-20）

## 结论

node-27 全国展示扩展方案已实施并上线。全国首页不再下载、解析约 45 MB 的
`national-basin-river.geojson`；18 个流域边界由轻量 domain GeoJSON 提供，基础河网和
可点击流量分别由两个 national MVT 图层按视窗加载。API 已由 user systemd 以 2 个 worker
托管，publish/coverage 后自动预热全国默认视野 z3/z4，跨 worker 文件缓存可稳定复用。

实施代码截至 `c3136132`；本回执后续只增加文档证据，不改变该运行代码。

## 实施内容

- migration `000048_river_segment_stream_type.sql` 增加持久生成的 `stream_type` 和索引；
  node-27 现有 209,126 个河段中 204,929 个可直接使用 1–5 级 Type。
- 新增 `/api/v1/tiles/river-network-national/{z}/{x}/{y}.pbf`；z3/z4 按 network/type
  聚合，避免低缩放 feature budget 413。
- national discharge 查询先按 tile/type 缩小集合，再关联有效时次流量；只有历史
  untyped 河段走分位回退。代表 z3 SQL 从改造前 3.735 s 降到 229.513 ms（约 16.3 倍）。
- 两类 national tile generation 均包含真实 active network/run 身份；文件缓存增加线程内
  和跨 uvicorn worker 的 single-flight。
- 前端只读取 `national-basin-domain.geojson`，基础河网使用 MapLibre vector source；
  run-scoped 图层目录不得覆盖 runless 的 time-less 全国基础图层。
- `/geo` 改为带 ETag/Last-Modified 的静态挂载；API 由
  `infra/systemd/nhms-display-api.service` 托管，默认 2 workers。
- `scripts/node27_mvt_prewarm.py` 以有限并发预热 z3/z4，并由
  `scripts/node27_autopipe_cron.sh` 在 coverage 后非致命调用。

## node-27 数据与服务证据

```text
deployed HEAD                         c3136132
nhms-display-api.service              enabled + active
display API MainPID / restarts        1493976 / 0
uvicorn workers                       2
nhms-node27-autopipe.timer            enabled + active
active basin/model networks           18
river_segment rows / typed rows       209126 / 204929
stream_type range                     1..5
migration elapsed                     14.254 s
```

Layer catalog 的当前代际：

```text
river-network-national:stream-type-aggregate-v1:212301fa7e64a565e2f8:18
hydro-national:344baabe138a2c9ed2e8:12
```

这里的 `:18` 是 18 个 active 基础河网；discharge 的 `:12` 是当前具备可发布流量产品、
可进入点击覆盖层的 12 个网络。没有产品的新流域仍显示基础河网，不生成假流量。

## MVT 预热、缓存与公网时延

同一 generation 第二次预热结果：

```json
{"request_count":26,"tile_count":13,"failed_count":0,"cache_hits":26,"bytes":422431,"zooms":[3,4],"valid_time":"2026-07-11T11:00:00Z"}
```

本机 warm hit：

```text
river-network-national median 0.012923 s, p95 0.014737 s
hydro-national         median 0.017243 s, p95 0.018061 s
```

从本地 Mac 经 `https://test.nwm.ac.cn` 连续 20 次 GET：

```text
river-network-national z3/6/3   median 0.324591 s, p95 0.342801 s, 62403 bytes
hydro-national z3/6/3           median 0.276246 s, p95 0.299959 s, 23439 bytes
```

两端点均返回 `200`、`X-Tile-Cache: hit` 和稳定 ETag；小于 300 KB 预算，公网 p95
小于 800 ms。基础河网 cold SQL 首次 918.182 ms、随后 670.818/661.577 ms；首次略高于
800 ms，但 z3/z4 属于部署/发布后预热集合，warm SQL 和实际 HTTP 均达标。discharge cold
SQL 为 229.513 ms，满足 800 ms 目标。

## 静态资源与浏览器证据

`national-basin-domain.geojson` 返回：

```text
Cache-Control: public, max-age=300, must-revalidate
ETag: "635c7d9dba1f6d00544b221d93b9d531"
If-None-Match 相同 ETag -> 304 Not Modified
```

使用全新浏览器会话访问生产入口，页面 DOM/React 运行态为：

```text
data-basin-feature-count          18
data-registered-overlays          discharge
data-national-river-source-type   vector
data-national-river-generation    river-network-national:stream-type-aggregate-v1:212301fa7e64a565e2f8:18
national-basin-river.geojson GET  0
```

浏览器渲染 feature 事件契约验证中，`m11-discharge-line-hit` 被分发后打开
“QHH 河段 773 / 河段 Q_DOWN 流量预报 · GFS+IFS”面板，并写入所选河段
`basins_qhh_shud_shud_riv_000773`。自动化浏览器运行环境没有 WebGL 上下文，因此无法从
GPU 像素命中测量“真实鼠标首次点击 p95”；本项不伪造数字。vector source 注册、交互层、
事件到弹窗链路与真实 MVT HTTP 数据面均已分别验证。

## 测试证据

```text
backend change-focused pytest                 103 passed, 383 deselected
frontend Vitest                               326 passed (33 files)
frontend check:api-types                      PASS
frontend production build                    PASS
ruff                                          PASS
shell bash -n / git diff --check              PASS
```

本次实现前的完整本地 `uv run pytest -q` 结果为 10,213 passed、140 skipped、22 failed；
其中 1 项是当时尚未修正的空 worker 参数测试，修正后已进入上述 focused PASS；另 1 项是
dirty-worktree evidence hash 检查，其余为 macOS 上依赖 Linux `/scratch`、GNU `stat` 或既有
scheduler strict-warm 断言的环境/既有失败。完整套件因此不记作全绿，不能替代上方范围测试
和 node-27 live oracle。

## 回滚点

- 前端可回退到实施前 bundle；旧 river GeoJSON 仍保留一个版本作为短期回滚资产。
- 可设置 `AUTOPIPE_MVT_PREWARM_ENABLED=0` 停用预热，不影响 ingest 结果。
- systemd 接管失败时可停止新 unit 并使用部署前 wrapper；当前 unit 已稳定 active，未触发回滚。
- migration 新列/索引对旧代码向后兼容；应用回滚不要求立即删除列。

## z5 放大 413 修复补充

首次上线后，浏览器放大到 z5 暴露
`river-network-national/5/25/12.pbf` 返回 413。服务端预算详情为 10,280 features，超过
10,000 上限；coordinate count 为 21,486，未超过 50,000。根因是 v1 只在 z3/z4 聚合，
从 z5 起过早恢复了逐河段 feature。

`584b04b0` 将 national 基础河网升级为 `stream-type-aggregate-v2`：所有仍在进行几何概化的
z≤8 均按 `river_network_version_id × Type` 聚合，z≥9 才恢复逐河段。基础河网是纯视觉层，
不在 interactive layer 列表中，因此合并 synthetic feature identity 不改变 discharge 点击、
河段身份或流量弹窗契约。预热范围同时从 z3/z4 扩到 z3/z4/z5。

node-27 修复后证据：

```text
原报错 URL             200, 65023 bytes, X-Tile-Cache=hit, 0.326817 s
source generation      river-network-national:stream-type-aggregate-v2:212301fa7e64a565e2f8:18
z3/z4/z5 prewarm       86 requests, 86 cache hits, 0 failures
z6 descendants         4 requests,  0 failures, max 50201 bytes
z7 descendants         16 requests, 0 failures, max 47411 bytes
z8 descendants         64 requests, 0 failures, max 52491 bytes
browser hard refresh   sourceType=vector, AJAXError=false, request413=false
related pytest         82 passed
```

扩大测试集合另有 13 项 macOS `/scratch`/Docker runtime 平台既有失败；相关 MVT、API、静态
资源、migration、预热测试均通过，ruff、`bash -n` 和 `git diff --check` 通过。
