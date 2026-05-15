## Context

SHUD-NWM 预报系统已完成 GFS 单源预报闭环（M1）、ERA5 分析运行与热启动（M2）、Slurm 全国化调度（M3）。当前架构天然支持多数据源：`met.data_source` 表存储适配器元数据，`hydro_run.scenario_id` 区分不同预报场景，API 已实现 `?scenarios=GFS,IFS` 多源过滤。

IFS 接入的主要技术挑战在于：(1) ECMWF Open Data 的数据格式和变量编码与 GFS 不同；(2) 06/18 周期仅提供 144h 预报（不足系统要求的 168h/7 天）；(3) 前端需要新增多源对比 UI。

**约束**：ECMWF Open Data 仅保留最近 12 个周期数据（约 2-3 天），必须及时镜像到对象存储。

## Goals / Non-Goals

**Goals**:
- IFS 数据自动发现、下载、转换、入库，与 GFS 流程对等
- IFS 预报通过现有编排器运行，结果以独立 scenario 存储
- 前端同一图表展示 GFS+IFS 双曲线，支持开关切换
- 06/18 周期可用时效在 UI 上明确标注

**Non-Goals**:
- 不实现 IFS 集合预报（仅 deterministic HRES）
- 不实现 GFS+IFS 融合预报（best_available 混合属于后续工作）
- 不修改数据库 schema（现有结构已满足）
- 不实现 CLDAS 接入（属于阶段 8A）

## Technical Decisions

### D1: IFS 适配器实现方式

**选型**：继承 `DataSourceAdapter` 基类，新建 `IFSAdapter` 类，使用 `ecmwf-opendata` 官方 Python 客户端。

**理由**：
- GFS 适配器通过 NOMADS HTTP 直接下载单变量 GRIB2 文件；IFS Open Data 提供批量检索 API，一次请求可下载多变量
- `ecmwf-opendata` 库封装了 ECMWF/AWS/Azure/Google 四个镜像源的自动切换，比自行实现 HTTP 下载更可靠
- 适配器接口（discover/manifest/download/verify）与 GFS/ERA5 一致，无需修改基类

**备选**：直接 HTTP 下载 ECMWF 公开 URL。缺点：URL 格式不稳定，无镜像切换能力。

### D2: IFS 变量映射策略

**选型**：在 `canonical_converter` 中新增 `IFS_VARIABLE_MAPPING` 和 `IFSCanonicalConverter` 子类。

| IFS 参数 | 标准变量 | 转换 |
|---|---|---|
| `2t` | `air_temperature_2m` | K → °C |
| `2d` + `2t` | `relative_humidity_2m` | Magnus 公式计算 RH |
| `tp` | `prcp_rate_or_amount` | m → mm，累积差分得 mm/step（forcing 阶段转 mm/day） |
| `10u`, `10v` | `wind_u_10m`, `wind_v_10m` | 直接使用（forcing 阶段合成 wind_speed_10m） |
| `sp` | `surface_pressure` | Pa |
| `ssr`, `str` | `net_radiation` | 累积 J/m² → W/m²，method=`direct_net` |

**关键差异**：
- GFS 直接提供 RH（rh2m），IFS 需从 T+Td 计算
- IFS 降水单位为 m（非 mm），需额外乘 1000
- IFS 辐射为净辐射（ssr+str），GFS 为下行分量（dswrf+dlwrf）

### D3: 06/18 周期不足 7 天处理

**选型**：在 adapter 和 forecast_cycle metadata 中标注 `max_lead_hours`，前端读取并显示。

**流程**：
1. `IFSAdapter` 根据 cycle_hour 设置 `forecast_end_hour`：00/12 → 168h，06/18 → 144h
2. manifest metadata 写入 `{"max_lead_hours": 144}`，同步记录到 `forcing_version.lineage_json`
3. API response 的 series 元素增加 `available_lead_hours` 和 `cycle_time` 字段（OpenAPI 需更新）
4. 编排器根据 `max_lead_hours` 动态设置 hydro_run 的 `end_time`（而非固定 168h）
5. 前端在 06/18 周期的曲线末端显示"6 天预报"标注

**不做**：不用 00/12 周期数据补齐 06/18 的缺失时段（避免混合不同起报时刻的预报）。

### D4: 编排器多源路由

**选型**：`OrchestratorConfig` 新增 `source_id` 字段，`scenario_id` 从 `source_id` 自动派生。

**映射规则**（将测试中的 `_scenario_for_source()` 迁移到生产代码 `services/orchestrator/chain.py`）：
```
GFS → forecast_gfs_deterministic
IFS → forecast_ifs_deterministic
```

**触发方式**：每个 IFS 周期完成 canonical 转换后，编排器以 `source_id=IFS` 启动 forcing → forecast → parse 链。GFS 和 IFS 的编排链独立运行、互不阻塞。

### D5: 前端多源对比设计

**选型**：在 ForecastPanel 顶部添加 scenario 复选框组，默认选中 GFS。

**视觉规范**：
- 分析运行（analysis）：蓝色实线 `#2266cc`
- GFS 预报：橙色实线 `#ef7d22`
- IFS 预报：绿色虚线 `#2ca02c`
- 06/18 周期：曲线末端添加虚线垂直标注 + "6d" 文字标签

**交互**：
- 复选框组：`☑ GFS ☐ IFS`，切换后重新请求 API（`?scenarios=GFS,IFS`）
- Tooltip 悬浮同时显示所有可见 scenario 的值
- 图例显示 scenario 名称和数据来源

## Risks and Mitigations

| 风险 | 影响 | 缓解 |
|---|---|---|
| ECMWF Open Data 服务不稳定 | IFS 周期下载失败 | 多镜像源自动切换（ecmwf → aws → azure → google）；失败不影响 GFS 预报 |
| IFS 数据延迟发布（比 GFS 晚数小时） | 同一起报时刻 IFS 曲线缺失 | 异步轮询，不阻塞 GFS 流程；前端缺失 IFS 时仅显示 GFS |
| RH 从 Td 计算引入误差 | forcing 质量下降 | 使用标准 Magnus 公式，误差 <1%（气象学界广泛使用） |
| 06/18 周期 6 天预报混淆用户 | 误判预报覆盖范围 | 前端强制标注可用时效；API 返回 available_lead_hours |
