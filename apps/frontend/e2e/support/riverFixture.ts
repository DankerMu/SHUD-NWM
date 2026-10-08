import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'

/**
 * 径流瓦片夹具的唯一导出（openspec mobile-responsive-display task 1.3）。
 *
 * 河段几何、四个身份字段、瓦片所属产品（源 / 周期 / 时次 / run）与瓦片坐标只写在 `fixtures/river-tile.fixture.json` 一处：
 * 生成脚本 `fixtures/generate-river-tile.mjs` 读它产出入库的 `.pbf`，本模块读它供 mock 与
 * `openRiverWindow` 使用。spec 里不要另写一份坐标或身份。
 */
export interface RiverFixture {
  /** 夹具瓦片坐标、MVT extent 与 source-layer 名。 */
  tile: { z: number; x: number; y: number; extent: number; sourceLayer: string }
  /** 瓦片里唯一一条河段的身份；`segment_id` 与 `river_segment_id` 同值。 */
  identity: { basinId: string; basinVersionId: string; riverNetworkVersionId: string; riverSegmentId: string }
  /**
   * 这张瓦片所属的产品：默认源的那个 run、在默认时次上。`runId` / `variable` / `unit` /
   * `qualityFlag` / `validTime` 原样写进要素属性；mock 的周期、run 与曲线单位都从这里取。
   */
  product: {
    source: 'gfs' | 'ifs'
    cycle: string
    validTime: string
    runId: string
    variable: string
    unit: string
    qualityFlag: string
  }
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
const { product } = riverFixture

/** 入库的最小矢量瓦片（由 `fixtures/generate-river-tile.mjs` 生成）。 */
export const riverTileFile = fixtureFile(`river-tile-${z}-${x}-${y}.pbf`)

/** 径流瓦片模板（与后端全国 source/cycle 目录同形）。 */
export const RIVER_TILE_URL_TEMPLATE = `/api/v1/tiles/hydro-national/{source}/{cycle}/${product.variable}/{valid_time}/{z}/{x}/{y}.pbf`

const DISCHARGE_TILE_PREFIX = '/api/v1/tiles/hydro-national/'

/** 夹具瓦片在其所属产品（源 / 周期 / 时次）下的 pathname——页面首屏默认请求的就是这一张。 */
export const riverFixtureTilePath = `${DISCHARGE_TILE_PREFIX}${product.source}/${product.cycle}/${product.variable}/${product.validTime}/${z}/${x}/${y}.pbf`

/** 全国径流瓦片请求（任意源 / 周期 / 时次 / 坐标）。入参是已解码的 pathname。 */
export function isDischargeTilePath(pathname: string): boolean {
  return pathname.startsWith(DISCHARGE_TILE_PREFIX) && pathname.endsWith('.pbf')
}

/** 径流瓦片 pathname 末三段的 `z/x/y`；不是径流瓦片时为 null。 */
export function dischargeTileCoordinates(pathname: string): { z: number; x: number; y: number } | null {
  if (!isDischargeTilePath(pathname)) return null
  const match = pathname.match(/\/(\d+)\/(\d+)\/(\d+)\.pbf$/)
  return match ? { z: Number(match[1]), x: Number(match[2]), y: Number(match[3]) } : null
}

/** 任意源 / 周期 / 时次下、坐标等于夹具瓦片的请求（URL 还带缓存版本 query，故只按 pathname 判）。 */
export function isRiverFixtureTilePath(pathname: string): boolean {
  const coordinates = dischargeTileCoordinates(pathname)
  return coordinates !== null && coordinates.z === z && coordinates.x === x && coordinates.y === y
}
