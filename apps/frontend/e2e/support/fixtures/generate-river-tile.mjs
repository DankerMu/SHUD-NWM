#!/usr/bin/env node
// 径流瓦片夹具生成器（openspec mobile-responsive-display task 1.3）。
//
//   cd apps/frontend && node e2e/support/fixtures/generate-river-tile.mjs [输出路径]
//
// 读同目录的 river-tile.fixture.json（河段几何、身份、所属产品与瓦片坐标的唯一来源），写出一张只含
// 该河段的 MVT。不带参数时覆盖入库文件 river-tile-<z>-<x>-<y>.pbf；带参数时写到该路径，
// 供 `cmp <再生成> <入库>` 核对可重复性。输出是输入的纯函数（无时间戳、无随机数）。
//
// 编码库 geojson-vt / vt-pbf 是 maplibre-gl 的传递依赖，本包没有直接声明，所以从
// maplibre-gl 的安装位置解析——不新增任何依赖。
import { readFileSync, realpathSync, writeFileSync } from 'node:fs'
import { createRequire } from 'node:module'
import { dirname, resolve } from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'

const here = dirname(fileURLToPath(import.meta.url))
const fixture = JSON.parse(readFileSync(resolve(here, 'river-tile.fixture.json'), 'utf8'))
const { z, x, y, extent, sourceLayer } = fixture.tile

const requireFromHere = createRequire(import.meta.url)
const requireFromMaplibre = createRequire(realpathSync(requireFromHere.resolve('maplibre-gl/package.json')))
const { default: geojsonvt } = await import(pathToFileURL(requireFromMaplibre.resolve('geojson-vt')).href)
const vtpbf = requireFromMaplibre('vt-pbf')

// 夹具自洽性：bbox 与河段必须整个落在声明的瓦片里，anchor 必须在河段上且贴近瓦片中心
// （钩子 fit 到 bbox 后视口以 anchor 为中心，贴近中心才能保证视口只落在这一张瓦片上）。
function tileFraction([lon, lat]) {
  const n = 2 ** z
  const latRad = (lat * Math.PI) / 180
  return [((lon + 180) / 360) * n - x, ((1 - Math.asinh(Math.tan(latRad)) / Math.PI) / 2) * n - y]
}
function fail(message) {
  console.error(`river-tile fixture is inconsistent: ${message}`)
  process.exit(1)
}
for (const point of [...fixture.bbox, ...fixture.line]) {
  const [fx, fy] = tileFraction(point)
  if (fx < 0 || fx > 1 || fy < 0 || fy > 1) fail(`${JSON.stringify(point)} is outside tile ${z}/${x}/${y}`)
}
const [anchorFx, anchorFy] = tileFraction(fixture.anchor)
if (Math.abs(anchorFx - 0.5) > 0.01 || Math.abs(anchorFy - 0.5) > 0.01) fail('anchor is not at the tile centre')
const [[startLon, startLat], [endLon, endLat]] = fixture.line
if (startLat !== endLat || fixture.anchor[1] !== startLat) fail('line must run along the anchor latitude')
if (!(startLon < fixture.anchor[0] && fixture.anchor[0] < endLon)) fail('anchor must lie inside the line')

const { basinId, basinVersionId, riverNetworkVersionId, riverSegmentId } = fixture.identity
const { product } = fixture
if (!/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$/.test(product.validTime)) fail('product.validTime must be YYYY-MM-DDTHH:MM:SSZ')
const index = geojsonvt(
  {
    type: 'FeatureCollection',
    features: [
      {
        type: 'Feature',
        geometry: { type: 'LineString', coordinates: fixture.line },
        // 属性集与取值类型照 services/tiles/mvt.py 的 hydro-national 分支：恰好这 12 个，
        // 除 value 是数值外都是字符串；feature_id = river_network_version_id || '::' || river_segment_id，
        // valid_time 是 YYYY-MM-DDTHH:MM:SSZ。生产瓦片没有 segment_name，这里也不写。
        properties: {
          feature_id: `${riverNetworkVersionId}::${riverSegmentId}`,
          segment_id: riverSegmentId,
          river_segment_id: riverSegmentId,
          river_network_version_id: riverNetworkVersionId,
          basin_version_id: basinVersionId,
          basin_id: basinId,
          value: fixture.value,
          unit: product.unit,
          quality_flag: product.qualityFlag,
          run_id: product.runId,
          variable: product.variable,
          valid_time: product.validTime,
        },
      },
    ],
  },
  { maxZoom: z, indexMaxZoom: z, tolerance: 0, extent, buffer: 64 },
)
const tile = index.getTile(z, x, y)
if (!tile || tile.features.length !== 1) fail(`expected exactly one feature in tile ${z}/${x}/${y}`)

const output = process.argv[2] ? resolve(process.argv[2]) : resolve(here, `river-tile-${z}-${x}-${y}.pbf`)
const bytes = Buffer.from(vtpbf.fromGeojsonVt({ [sourceLayer]: tile }, { version: 2, extent }))
writeFileSync(output, bytes)
console.log(`wrote ${output} (${bytes.length} bytes)`)
