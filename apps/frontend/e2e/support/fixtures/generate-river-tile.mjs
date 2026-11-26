#!/usr/bin/env node
// 径流瓦片夹具生成器（openspec mobile-responsive-display task 1.3；shared-reach-click-ownership task 4.6）。
//
//   cd apps/frontend && node e2e/support/fixtures/generate-river-tile.mjs [输出路径]
//   cd apps/frontend && node e2e/support/fixtures/generate-river-tile.mjs --shared-reach [输出目录]
//
// 第一种：读同目录的 river-tile.fixture.json（河段几何、身份、所属产品与瓦片坐标的唯一来源），写出一张只含
// 该河段的 MVT。不带参数时覆盖入库文件 river-tile-<z>-<x>-<y>.pbf；带参数时写到该路径，
// 供 `cmp <再生成> <入库>` 核对可重复性。
//
// 第二种（#3390）：另读第二份描述 shared-reach-tile.fixture.json——第二个河网的身份，以及若干张瓦片各自的
// 要素顺序（按 river_network_version_id 列出）。几何、所属产品与瓦片坐标沿用 river-tile.fixture.json：
// 两个河网的河段完全重合。每张瓦片按声明的顺序写出这些要素，文件名 shared-reach-<name>-<z>-<x>-<y>.pbf；
// 不带目录参数时写到本目录（覆盖入库文件），带参数时写到该目录。
//
// 输出是输入的纯函数（无时间戳、无随机数）。编码库 geojson-vt / vt-pbf 是 maplibre-gl 的传递依赖，
// 本包没有直接声明，所以从 maplibre-gl 的安装位置解析——不新增任何依赖。
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

const { product } = fixture
if (!/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$/.test(product.validTime)) fail('product.validTime must be YYYY-MM-DDTHH:MM:SSZ')

/** 一条河段要素：几何取夹具河段，身份与 value 取入参。 */
function segmentFeature({ basinId, basinVersionId, riverNetworkVersionId, riverSegmentId }, value) {
  return {
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
      value,
      unit: product.unit,
      quality_flag: product.qualityFlag,
      run_id: product.runId,
      variable: product.variable,
      valid_time: product.validTime,
    },
  }
}

/** 按给定顺序把要素切进夹具瓦片；瓦片里的要素必须恰好是这些、且顺序不变。 */
function encodeTile(features) {
  const index = geojsonvt(
    { type: 'FeatureCollection', features },
    { maxZoom: z, indexMaxZoom: z, tolerance: 0, extent, buffer: 64 },
  )
  const tile = index.getTile(z, x, y)
  if (!tile || tile.features.length !== features.length) {
    fail(`expected exactly ${features.length === 1 ? 'one feature' : `${features.length} features`} in tile ${z}/${x}/${y}`)
  }
  const emitted = tile.features.map((feature) => feature.tags.feature_id)
  const declared = features.map((feature) => feature.properties.feature_id)
  if (JSON.stringify(emitted) !== JSON.stringify(declared)) {
    fail(`tile feature order ${JSON.stringify(emitted)} differs from the declared ${JSON.stringify(declared)}`)
  }
  return Buffer.from(vtpbf.fromGeojsonVt({ [sourceLayer]: tile }, { version: 2, extent }))
}

function write(output, bytes) {
  writeFileSync(output, bytes)
  console.log(`wrote ${output} (${bytes.length} bytes)`)
}

if (process.argv[2] === '--shared-reach') {
  const shared = JSON.parse(readFileSync(resolve(here, 'shared-reach-tile.fixture.json'), 'utf8'))
  const segments = new Map([
    [fixture.identity.riverNetworkVersionId, segmentFeature(fixture.identity, fixture.value)],
    [shared.second.identity.riverNetworkVersionId, segmentFeature(shared.second.identity, shared.second.value)],
  ])
  if (segments.size !== 2) fail('the second network must differ from the first')
  const outputDir = process.argv[3] ? resolve(process.argv[3]) : here
  for (const { name, order } of shared.tiles) {
    if (order.length !== segments.size || new Set(order).size !== order.length || !order.every((id) => segments.has(id))) {
      fail(`tile ${name} must list each of ${JSON.stringify([...segments.keys()])} exactly once`)
    }
    write(resolve(outputDir, `shared-reach-${name}-${z}-${x}-${y}.pbf`), encodeTile(order.map((id) => segments.get(id))))
  }
} else {
  const output = process.argv[2] ? resolve(process.argv[2]) : resolve(here, `river-tile-${z}-${x}-${y}.pbf`)
  write(output, encodeTile([segmentFeature(fixture.identity, fixture.value)]))
}
