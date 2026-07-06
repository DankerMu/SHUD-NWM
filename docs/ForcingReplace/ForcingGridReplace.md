# SHUD 气象驱动格网替换指南

> 适用场景：模型建模完成后，将气象驱动从一套格网（如 CMFD）替换为另一套格网（如 GFS）。  
> 无需重建网格，无需 DEM，只需更新 **att 文件的 FORC 列** 和 **tsd.forc 列表**。  
> 气象 CSV 文件由外部程序单独制备。

> **适用边界（重要）**：本文只适用于 **standalone SHUD 手工工作流**（本地/科研环境，人工制备 CSV）。
> SHUD-NWM 生产系统的迁移**不得**按本文操作：格点必须来自 canonical grid registry（不得 `seq()` 手造）、
> 周期 forcing 由 producer 每 cycle 动态生成（不得离线写死 startdate）、资产必须走不可变发布与验证门。
> 生产迁移规范见同目录《CMFD 建模资产向 IFSGFS Direct-Grid 的安全迁移》。

---

## 三个文件的关系

```
input/prj/prj.sp.att      →  FORC 列（整数，1-based 索引）
                                    ↓
input/forc/prj.tsd.forc   →  第 FORC 行（GFS 格点坐标 + CSV 文件名）
                                    ↓
input/forc/<格点>.csv      →  该格点的气象时间序列（由外部程序制备）
```

---

## 替换步骤

### Step 1：读入三角网格，计算质心

```r
library(sf)
library(rSHUD)

# 读入已建好的三角网格 shapefile
domain <- sf::st_read("input/prj/gis/domain.shp")

# 计算每个三角形的质心（投影坐标系）
centroids_pcs <- sf::st_centroid(domain)

# 转换为 GCS（WGS84），便于与 GFS 经纬度格点匹配
centroids_gcs <- sf::st_transform(centroids_pcs, crs = 4326)
```

> **几何权威提示**：`domain.shp` 的行序不保证永远等于 `.sp.att` 的 `INDEX`。rSHUD 原生导出时两者同序，
> 本文的 standalone 场景依赖这一点；若 shapefile 被编辑/重排过，必须改从 `.sp.mesh` 的
> element→3 顶点直接计算重心（节点 X/Y 即投影坐标），再转 WGS84。

---

### Step 2：定义新格网（GFS）格点

```r
# 按实际 GFS 格点坐标填入（WGS84 经纬度）
gfs_coords <- expand.grid(
  lon = seq(98.0, 104.0, by = 0.25),
  lat = seq(28.0,  34.0, by = 0.25)
)

sp.gfs <- sf::st_as_sf(
  gfs_coords,
  coords = c("lon", "lat"),
  crs    = 4326
)

# 格点编号（1-based，决定 tsd.forc 中的行顺序）
sp.gfs$ID       <- seq_len(nrow(sp.gfs))
sp.gfs$Filename <- paste0("GFS_X", round(gfs_coords$lon, 2),
                          "Y",     round(gfs_coords$lat, 2), ".csv")
```

---

### Step 3：空间匹配——为每个三角形找最近 GFS 格点

```r
# 最近邻匹配：返回每个三角质心对应的 GFS 格点行号（即 GFS 的 ID）
# 注意：sf >= 1.0 对经纬度坐标默认用 s2 球面距离（geodesic）。
# 不要 sf_use_s2(FALSE)，否则退化为平面度数距离。
nearest_idx <- sf::st_nearest_feature(centroids_gcs, sp.gfs)

# nearest_idx[i] = 第 i 个三角形对应的 GFS 格点编号（1-based）

# used-cell 子集：只保留被至少一个三角形引用的格点，并重编为连续 1..N。
# 不做这一步会把 bbox 内全部格点写成 station，产生大量永远不被 FORC 引用的
# 无用 CSV（实测既有模型资产普遍存在此问题：11/13 流域带 unused station）。
used        <- sort(unique(nearest_idx))
remap       <- match(seq_len(nrow(sp.gfs)), used)   # 旧 ID -> 新 ID（未引用为 NA）
nearest_idx <- remap[nearest_idx]
sp.gfs      <- sp.gfs[used, ]
sp.gfs$ID   <- seq_len(nrow(sp.gfs))
```

---

### Step 4：更新 .att 文件的 FORC 列

```r
fin <- shud.filein(xfg$prjname,
                   inpath  = xfg$dir$modelin,
                   outpath = xfg$dir$modelout)

# 读入现有 att 文件
pa <- read_df(fin["md.att"])[[1]]

# 替换 FORC 列
pa[, "FORC"] <- as.integer(nearest_idx)

# 写回
write_df(pa, file = fin["md.att"])
message("att 文件已更新，共 ", nrow(pa), " 个三角形。")
```

---

### Step 5：更新 tsd.forc 文件

```r
# 构建 tsd.forc 所需的 data.frame（含坐标和文件名）
coords_gcs <- sf::st_coordinates(sp.gfs)
forc_table <- data.frame(
  ID       = sp.gfs$ID,
  Lon      = coords_gcs[, "X"],
  Lat      = coords_gcs[, "Y"],
  Filename = sp.gfs$Filename
)

new_forc_dir <- file.path(xfg$dir$forc, "gfs")
dir.create(new_forc_dir, recursive = TRUE, showWarnings = FALSE)

write_forc(
  x         = forc_table,
  file      = fin["md.forc"],
  path      = new_forc_dir,
  startdate = paste0(min(xfg$years), "0101")
)
message("tsd.forc 已更新，共 ", nrow(forc_table), " 个 GFS 格点。")
```

---

### Step 6：制备气象 CSV 文件（外部程序）

`new_forc_dir` 目录下，每个格点对应一个 CSV 文件，文件名与 `forc_table$Filename` 一致。  
文件格式为 SHUD TSD 格式，由外部程序（Python/R/其他）单独生成：

```
N_rows  N_cols  YYYYMMDD
TIME    Prec    Temp    RH      Wind    Srad
0.000   0.001   285.2   0.65    2.1     120.5
0.042   0.000   284.8   0.67    1.9     0.0
...
```

- `TIME`：距起始日期的天数（浮点）
- 列名与 SHUD 配置文件约定一致
- 文件制备完成后，直接运行 SHUD 即可

---

### Step 7：绘制替换前后对比图

> **坐标系注意**：`domain.shp` 使用建模时定义的**投影坐标系（PCS）**，而 tsd.forc 中的站点坐标（Lon/Lat）和 GFS 格点均为**地理坐标系 WGS84（GCS）**。  
> 绘图前必须将所有站点统一转换到 domain 的 PCS，否则叠加位置会出现严重偏差。

```r
library(ggplot2)

# —— 读取替换前数据 ——
# 从 tsd.forc 读取原始站点（GCS 经纬度）
forc.old.info  <- read_forc_fn(fin["md.forc"])$Sites   # 替换前先执行此行读取！
# 注意：若已执行 write_forc() 覆盖，请从备份文件读取
# forc.old.info <- read_forc_fn("backup/prj.tsd.forc.bak")$Sites

domain_pcs <- sf::st_read(file.path(xfg$dir$modelin, "gis", "domain.shp"))
domain_crs <- sf::st_crs(domain_pcs)   # domain 的 PCS

# 原始站点：GCS → PCS
sites.old.gcs <- sf::st_as_sf(forc.old.info,
                               coords = c("Lon", "Lat"), crs = 4326)
sites.old.pcs <- sf::st_transform(sites.old.gcs, crs = domain_crs)

# 新站点（sp.gfs）：GCS → PCS
sites.new.pcs <- sf::st_transform(sp.gfs, crs = domain_crs)

# —— 准备绘图用 data.frame ——
# domain 几何 + 原始 FORC 值（替换前读取的 pa.old）
domain_plot <- domain_pcs
domain_plot$FORC_old <- as.factor(pa.old[, "FORC"])   # pa.old：替换前读取的 att
domain_plot$FORC_new <- as.factor(pa.new[, "FORC"])   # pa.new：替换后的 att

coords.old <- sf::st_coordinates(sites.old.pcs)
coords.new <- sf::st_coordinates(sites.new.pcs)

df.sites.old <- data.frame(X = coords.old[,"X"], Y = coords.old[,"Y"],
                            ID = forc.old.info$ID)
df.sites.new <- data.frame(X = coords.new[,"X"], Y = coords.new[,"Y"],
                            ID = sites.new.pcs$ID)

# —— 绘图 ——
p1 <- ggplot() +
  geom_sf(data = domain_plot, aes(fill = FORC_old), color = NA) +
  geom_point(data = df.sites.old, aes(x = X, y = Y),
             shape = 3, size = 2, color = "black") +
  scale_fill_viridis_d(option = "turbo", guide = "none") +
  labs(title = "替换前（原始格网）",
       subtitle = paste0("格点数 = ", nlevels(domain_plot$FORC_old))) +
  theme_bw() + theme(axis.title = element_blank())

p2 <- ggplot() +
  geom_sf(data = domain_plot, aes(fill = FORC_new), color = NA) +
  geom_point(data = df.sites.new, aes(x = X, y = Y),
             shape = 3, size = 2, color = "black") +
  scale_fill_viridis_d(option = "turbo", guide = "none") +
  labs(title = "替换后（新格网）",
       subtitle = paste0("格点数 = ", nlevels(domain_plot$FORC_new))) +
  theme_bw() + theme(axis.title = element_blank())

# 拼图输出
library(patchwork)
fig <- p1 + p2 +
  plot_annotation(title = "气象驱动格网替换前后对比",
                  caption = paste0("三角形颜色 = FORC 编号；+ = 格点位置（均已转换至 PCS）"))

fig.path <- file.path(xfg$dir$fig, "forc_replace_compare.png")
ggsave(fig.path, fig, width = 14, height = 7, dpi = 150)
message("对比图已保存：", fig.path)
```

**输出示例说明：**

| 元素 | 说明 |
|------|------|
| 三角形填色 | 同色三角形 = 同一个气象格点驱动，颜色区块即 Voronoi 归属 |
| `+` 标记 | 各气象格点的实际位置（已转换至 PCS） |
| 子图标题 | 显示实际使用的格点数量 |

> **脚本执行顺序提示**：读取 `pa.old` 和 `forc.old.info` 必须在调用 `write_df()` / `write_forc()` **之前**执行，否则需从备份文件中恢复。

---

## 完整替换脚本

保存为 `SubScript/Sub_ForcingReplace.R`，使用前先 `source('GetReady.R')`：

```r
# SubScript/Sub_ForcingReplace.R
# 替换气象驱动格网：更新 att（FORC 列）和 tsd.forc，并输出对比图
# 不重建网格，不需要 DEM；气象 CSV 文件由外部程序单独制备
# 使用前先 source('GetReady.R')

library(sf)
library(ggplot2)
library(patchwork)
library(rSHUD)

# ——— 配置 ———
new_forc_dir <- file.path(xfg$dir$forc, "gfs")
dir.create(new_forc_dir, recursive = TRUE, showWarnings = FALSE)

fin <- shud.filein(xfg$prjname,
                   inpath  = xfg$dir$modelin,
                   outpath = xfg$dir$modelout)

# Step 1: 读入三角网格，获取 PCS 和 GCS 质心
# domain.shp 使用建模时定义的 PCS，GFS 格点为 GCS（WGS84）
# 绘图时需统一转换到同一坐标系
domain     <- sf::st_read(file.path(xfg$dir$modelin, "gis", "domain.shp"))
domain_crs <- sf::st_crs(domain)   # 建模 PCS，后续站点坐标均需转换至此
centroids_gcs <- sf::st_transform(sf::st_centroid(domain), crs = 4326)

# Step 2: 定义 GFS 格点（按实际范围修改，GCS WGS84）
gfs_coords <- expand.grid(
  lon = seq(98.0, 104.0, by = 0.25),
  lat = seq(28.0,  34.0, by = 0.25)
)
sp.gfs <- sf::st_as_sf(gfs_coords, coords = c("lon", "lat"), crs = 4326)
sp.gfs$ID       <- seq_len(nrow(sp.gfs))
sp.gfs$Filename <- paste0("GFS_X", round(gfs_coords$lon, 2),
                          "Y",     round(gfs_coords$lat, 2), ".csv")

# Step 3: 最近邻匹配（s2 geodesic 默认；不要关闭）+ used-cell 子集
nearest_idx <- sf::st_nearest_feature(centroids_gcs, sp.gfs)
used        <- sort(unique(nearest_idx))
remap       <- match(seq_len(nrow(sp.gfs)), used)
nearest_idx <- remap[nearest_idx]
sp.gfs      <- sp.gfs[used, ]
sp.gfs$ID   <- seq_len(nrow(sp.gfs))

# Step 4: 读取替换前数据（必须在 write 之前！）
pa.old         <- read_df(fin["md.att"])[[1]]
forc.old.info  <- read_forc_fn(fin["md.forc"])$Sites

# Step 5: 替换 FORC 列并写回 att
pa.new           <- pa.old
pa.new[, "FORC"] <- as.integer(nearest_idx)
write_df(pa.new, file = fin["md.att"])

# Step 6: 更新 tsd.forc
coords_gcs <- sf::st_coordinates(sp.gfs)
forc_table <- data.frame(
  ID       = sp.gfs$ID,
  Lon      = coords_gcs[, "X"],
  Lat      = coords_gcs[, "Y"],
  Filename = sp.gfs$Filename
)
write_forc(forc_table, file = fin["md.forc"],
           path      = new_forc_dir,
           startdate = paste0(min(xfg$years), "0101"))

message("att 和 tsd.forc 已更新。请在 ", new_forc_dir, " 中制备气象 CSV 后运行 SHUD。")

# Step 7: 对比图
# 所有站点坐标从 GCS 转换到 domain 的 PCS，与 domain.shp 叠加
sites.old.pcs <- sf::st_transform(
  sf::st_as_sf(forc.old.info, coords = c("Lon","Lat"), crs = 4326),
  crs = domain_crs)
sites.new.pcs <- sf::st_transform(sp.gfs, crs = domain_crs)

domain$FORC_old <- as.factor(pa.old[, "FORC"])
domain$FORC_new <- as.factor(pa.new[, "FORC"])

df.old <- as.data.frame(sf::st_coordinates(sites.old.pcs))
df.new <- as.data.frame(sf::st_coordinates(sites.new.pcs))

p1 <- ggplot() +
  geom_sf(data = domain, aes(fill = FORC_old), color = NA) +
  geom_point(data = df.old, aes(x = X, y = Y), shape = 3, size = 2) +
  scale_fill_viridis_d(option = "turbo", guide = "none") +
  labs(title = "替换前（原始格网）",
       subtitle = paste0("格点数 = ", nlevels(domain$FORC_old))) +
  theme_bw() + theme(axis.title = element_blank())

p2 <- ggplot() +
  geom_sf(data = domain, aes(fill = FORC_new), color = NA) +
  geom_point(data = df.new, aes(x = X, y = Y), shape = 3, size = 2) +
  scale_fill_viridis_d(option = "turbo", guide = "none") +
  labs(title = "替换后（新格网）",
       subtitle = paste0("格点数 = ", nlevels(domain$FORC_new))) +
  theme_bw() + theme(axis.title = element_blank())

fig <- p1 + p2 +
  plot_annotation(
    title   = "气象驱动格网替换前后对比",
    caption = "三角形颜色 = FORC 编号（同色区块由同一格点驱动）；+ = 格点位置（已统一转换至建模 PCS）"
  )

fig.path <- file.path(xfg$dir$fig, "forc_replace_compare.png")
ggsave(fig.path, fig, width = 14, height = 7, dpi = 150)
message("对比图已保存：", fig.path)
```

---

## 注意事项

| 项目 | 说明 |
|------|------|
| **坐标系差异** ⚠️ | `domain.shp` 为建模 PCS（投影坐标），GFS/CMFD 格点为 GCS WGS84（经纬度）。空间匹配（Step 3）统一在 GCS 下进行；绘图叠加（Step 7）须将格点转换至 PCS，否则叠加位置严重偏差 |
| 无需 DEM | 质心从 domain.shp 直接计算，不涉及高程 |
| 格点数可变 | 新旧格网格点数不同完全没问题，FORC 索引自动重映射 |
| 只替换 FORC 列 | att 文件的 SOIL/GEOL/LC 等列保持不变 |
| 先读后写 | `pa.old` 和 `forc.old.info` 必须在 `write_df()` / `write_forc()` 之前读取，用于对比图 |
| CSV 文件名 | 必须与 `forc_table$Filename` 严格一致 |
| 起始日期 | `write_forc` 的 `startdate` 与 CSV 起始时间保持一致 |
| 备份 | 替换前建议备份原 att 和 tsd.forc |
| **used-cell 子集** | 只保留被 FORC 实际引用的格点并重编连续 1..N；不要把 bbox 内全部格点写成 station |
| 行序 ≠ ID | 不依赖 domain.shp 行序等于 .sp.att INDEX；shapefile 被改动过时改用 .sp.mesh 顶点算质心 |
| 距离度量 | 经纬度最近邻保持 sf/s2 球面距离默认，不要 `sf_use_s2(FALSE)` |
| **生产系统边界** | SHUD-NWM 生产迁移不得按本文操作（格点须来自 canonical registry、forcing 由 producer 每 cycle 生成），见同目录安全迁移规范 |
