import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'

/**
 * 径流瓦片夹具的唯一导出（openspec mobile-responsive-display task 1.3）。
 *
 * 河段几何、四个身份字段与瓦片坐标只写在 `fixtures/river-tile.fixture.json` 一处：
 * 生成脚本 `fixtures/generate-river-tile.mjs` 读它产出入库的 `.pbf`，本模块读它供 mock 与
 * `openRiverWindow` 使用。spec 里不要另写一份坐标或身份。
 */
export interface RiverFixture {
  /** 夹具瓦片坐标、MVT extent 与 source-layer 名。 */
  tile: { z: number; x: number; y: number; extent: number; sourceLayer: string }
  /** 瓦片里唯一一条河段的身份；`segment_id` 与 `river_segment_id` 同值。 */
  identity: { basinId: string; basinVersionId: string; riverNetworkVersionId: string; riverSegmentId: string }
  segmentName: string
  /** 要素的 `value` 属性（q_down，m3/s）。 */
  value: number
  /** 河段中点，也是瓦片中心：钩子 fit 到 bbox 后它落在地图区中央。 */
  anchor: [number, number]
  /** `[[minLon, minLat], [maxLon, maxLat]]`，整个落在夹具瓦片内。 */
  bbox: [[number, number], [number, number]]
  /** 河段两端点（沿 anchor 所在纬线）。 */
  line: [[number, number], [number, number]]
}

function fixtureFile(name: string): string {
  return fileURLToPath(new URL(`./fixtures/${name}`, import.meta.url))
}

export const riverFixture = JSON.parse(readFileSync(fixtureFile('river-tile.fixture.json'), 'utf8')) as RiverFixture

const { z, x, y } = riverFixture.tile

/** 入库的最小矢量瓦片（由 `fixtures/generate-river-tile.mjs` 生成）。 */
export const riverTileFile = fixtureFile(`river-tile-${z}-${x}-${y}.pbf`)

/** 径流瓦片模板（与后端全国 source/cycle 目录同形）。 */
export const RIVER_TILE_URL_TEMPLATE = '/api/v1/tiles/hydro-national/{source}/{cycle}/q_down/{valid_time}/{z}/{x}/{y}.pbf'

/** 任意源 / 周期 / 时次下的夹具瓦片：只按 pathname 匹配（URL 还带缓存版本 query）。 */
const riverFixtureTilePath = new RegExp(`^/api/v1/tiles/hydro-national/(gfs|ifs)/[^/]+/q_down/[^/]+/${z}/${x}/${y}\\.pbf$`)

export function isRiverFixtureTilePath(pathname: string): boolean {
  return riverFixtureTilePath.test(pathname)
}
