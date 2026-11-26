import { describe, expect, it, vi } from 'vitest'
import type { MapLayerMouseEvent, MapRef } from 'react-map-gl/maplibre'

import {
  chooseM11OverlayHitFeature,
  handleM11MapClick,
  handleM11MapMouseMove,
  type M11NetworkSegmentCounts,
} from '@/components/map/m11MapInteractions'
import type { M11RegisteredOverlay } from '@/components/map/m11MapBuilders'
import { M11_BASIN_FILL_LAYER_ID, MET_STATION_POINT_LAYER_ID } from '@/components/map/m11MapPrimitives'

/**
 * shared-reach-click-ownership (#3390): on a reach carried by two basin networks the click (and the
 * hover) goes to the network with fewer segments when every network under the pointer has a count;
 * otherwise, and on ties, to the larger `river_network_version_id` (code-unit order); features of one
 * network keep the map's order. Expected values come from the spec delta, not from the implementation.
 */

const OVERLAY = {
  layerId: 'discharge',
  sourceId: 'm11-discharge-source',
  sourceKey: 'k',
  layer: { id: 'm11-discharge-line' },
  source: { type: 'vector', tiles: [], sourceLayer: 'hydro', minzoom: 0, maxzoom: 14, metadata: {} },
} as unknown as M11RegisteredOverlay
const HIT = 'm11-discharge-line-hit'

type TestFeature = { layer: { id: string }; properties: Record<string, unknown>; geometry: { type: string; coordinates: number[] } }

function segment(networkId: string | undefined, segmentId: string): TestFeature {
  const properties: Record<string, unknown> = { river_segment_id: segmentId, segment_id: segmentId }
  if (networkId !== undefined) properties.river_network_version_id = networkId
  return { layer: { id: HIT }, properties, geometry: { type: 'LineString', coordinates: [101, 31] } }
}

function other(layerId: string): TestFeature {
  return { layer: { id: layerId }, properties: { marker: layerId }, geometry: { type: 'Point', coordinates: [101, 31] } }
}

function mouseEvent(features: unknown[]): MapLayerMouseEvent {
  return {
    features,
    point: { x: 10, y: 20 },
    lngLat: { lng: 101, lat: 31 },
    target: { getCanvas: () => ({ style: { cursor: '' } }) },
  } as unknown as MapLayerMouseEvent
}

const mapRef = { getMap: () => ({ queryRenderedFeatures: () => [], flyTo: vi.fn(), getSource: () => undefined }) } as unknown as MapRef

/** What a product click at a point with these features opens (the feature handed to onOverlayClick). */
function clickOpens(features: unknown[], segmentCounts: M11NetworkSegmentCounts | null, showStationLayer = false) {
  const onOverlayClick = vi.fn()
  handleM11MapClick(mouseEvent(features), { showStationLayer, renderableOverlay: OVERLAY, segmentCounts, mapRef, onOverlayClick })
  expect(onOverlayClick).toHaveBeenCalledTimes(1)
  return onOverlayClick.mock.calls[0][0] as { layerId: string; feature: unknown }
}

/** Both orders in which the map can return two features. */
function bothOrders<T>(a: T, b: T): T[][] {
  return [[a, b], [b, a]]
}

// The owner scenario's networks: the one with fewer segments has the SMALLER id, so the id alone
// (today's draw order) would choose the other one and an implementation that ignores counts fails.
const NESTED = 'basins_byh_rivnet'
const LARGER = 'basins_cj_main_rivnet'
const OWNER_COUNTS = { [NESTED]: 7971, [LARGER]: 25971 }

describe('On a reach shared by two basin networks the click opens the segment of the network with fewer segments', () => {
  it('the nested network owns the shared reach, in either order the map returns the two features', () => {
    const nested = segment(NESTED, 'seg-byh')
    const larger = segment(LARGER, 'seg-cj')
    expect(NESTED < LARGER).toBe(true)
    for (const order of bothOrders(nested, larger)) {
      const opened = clickOpens(order, OWNER_COUNTS)
      expect(opened.layerId).toBe('discharge')
      expect(opened.feature).toBe(nested)
    }
  })

  it('one network under the pointer behaves as before: the first one the map returned', () => {
    const first = segment(NESTED, 'seg-1')
    const second = segment(NESTED, 'seg-2')
    expect(clickOpens([first, second], OWNER_COUNTS).feature).toBe(first)
    expect(clickOpens([second, first], OWNER_COUNTS).feature).toBe(second)
    expect(clickOpens([first, second], null).feature).toBe(first)
  })

  it('equal counts are decided by the larger network id, in either order', () => {
    const b = segment('rn-b', 'seg-b')
    const c = segment('rn-c', 'seg-c')
    for (const order of bothOrders(b, c)) {
      expect(clickOpens(order, { 'rn-b': 500, 'rn-c': 500 }).feature).toBe(c)
    }
  })

  it('one network without a count switches the counts off: the larger id, whichever of the two has the count', () => {
    const nested = segment(NESTED, 'seg-byh')
    const larger = segment(LARGER, 'seg-cj')
    const onlyOne: M11NetworkSegmentCounts[] = [{ [NESTED]: 7971 }, { [LARGER]: 25971 }]
    for (const counts of onlyOne) {
      for (const order of bothOrders(nested, larger)) {
        expect(clickOpens(order, counts).feature).toBe(larger)
      }
    }
  })

  it('no counts loaded yet: the larger network id', () => {
    const nested = segment(NESTED, 'seg-byh')
    const larger = segment(LARGER, 'seg-cj')
    for (const counts of [null, {}]) {
      for (const order of bothOrders(nested, larger)) {
        expect(clickOpens(order, counts).feature).toBe(larger)
      }
    }
  })

  it('a station still wins over a river when stations are shown', () => {
    const station = other(MET_STATION_POINT_LAYER_ID)
    const nested = segment(NESTED, 'seg-byh')
    const larger = segment(LARGER, 'seg-cj')
    for (const rivers of bothOrders(nested, larger)) {
      const opened = clickOpens([...rivers, station], OWNER_COUNTS, true)
      expect(opened).toMatchObject({ layerId: 'met-stations' })
      expect(opened.feature).toBe(station)
    }
  })
})

describe('hover shows what the click opens on a shared reach', () => {
  it('hands onOverlayHover the nested network feature (the map object itself), in either order, and a click there opens it', () => {
    const nested = segment(NESTED, 'seg-byh')
    const larger = segment(LARGER, 'seg-cj')
    const basin = other(M11_BASIN_FILL_LAYER_ID)
    for (const rivers of bothOrders(nested, larger)) {
      const onOverlayHover = vi.fn()
      const event = mouseEvent([basin, ...rivers])
      handleM11MapMouseMove(event, { showStationLayer: false, renderableOverlay: OVERLAY, segmentCounts: OWNER_COUNTS, mapRef, onOverlayHover })
      expect(onOverlayHover).toHaveBeenCalledTimes(1)
      expect(onOverlayHover.mock.calls[0][0]).toMatchObject({ layerId: 'discharge', event })
      expect(onOverlayHover.mock.calls[0][0].feature).toBe(nested)
      expect(clickOpens([basin, ...rivers], OWNER_COUNTS).feature).toBe(nested)
    }
  })

  it('without counts the hover follows the larger id, like the click', () => {
    const nested = segment(NESTED, 'seg-byh')
    const larger = segment(LARGER, 'seg-cj')
    for (const rivers of bothOrders(nested, larger)) {
      const onOverlayHover = vi.fn()
      handleM11MapMouseMove(mouseEvent(rivers), { showStationLayer: false, renderableOverlay: OVERLAY, segmentCounts: null, mapRef, onOverlayHover })
      expect(onOverlayHover.mock.calls[0][0].feature).toBe(larger)
    }
  })
})

describe('chooseM11OverlayHitFeature', () => {
  const nested = segment('rn-b', 'seg-b')
  const larger = segment('rn-c', 'seg-c')

  it('returns null when no feature is on the hit layer', () => {
    expect(chooseM11OverlayHitFeature([other(M11_BASIN_FILL_LAYER_ID)], HIT, { 'rn-b': 1 })).toBeNull()
    expect(chooseM11OverlayHitFeature([], HIT, null)).toBeNull()
    expect(chooseM11OverlayHitFeature(undefined, HIT, null)).toBeNull()
  })

  it.each([
    ['zero', 0],
    ['negative', -5],
    ['NaN', Number.NaN],
    ['Infinity', Number.POSITIVE_INFINITY],
    ['missing', undefined],
  ])('an unusable count (%s) on either network switches the counts off for the whole candidate set', (_label, bad) => {
    const withBad = (networkId: string, good: Record<string, number>) => {
      const counts: Record<string, number> = { ...good }
      if (bad !== undefined) counts[networkId] = bad
      return counts
    }
    // Bad count on the nested network: treating it as 0 would wrongly pick the nested one.
    const badOnNested = withBad('rn-b', { 'rn-c': 25971 })
    // Bad count on the larger network: comparing only the usable counts would wrongly pick the nested one.
    const badOnLarger = withBad('rn-c', { 'rn-b': 7971 })
    for (const counts of [badOnNested, badOnLarger]) {
      for (const order of bothOrders(nested, larger)) {
        expect(chooseM11OverlayHitFeature(order, HIT, counts)).toBe(larger)
      }
    }
    // Control: with both counts usable the nested network wins.
    for (const order of bothOrders(nested, larger)) {
      expect(chooseM11OverlayHitFeature(order, HIT, { 'rn-b': 7971, 'rn-c': 25971 })).toBe(nested)
    }
  })

  it('a feature without a network id comes last and switches the counts off', () => {
    const anonymous = segment(undefined, 'seg-anon')
    const emptyId = { ...segment(undefined, 'seg-empty'), properties: { river_network_version_id: '' } }
    const counts = { 'rn-b': 7971, 'rn-c': 25971 }
    expect(chooseM11OverlayHitFeature([anonymous, nested], HIT, null)).toBe(nested)
    expect(chooseM11OverlayHitFeature([emptyId, nested], HIT, counts)).toBe(nested)
    // Counts are off (the anonymous candidate has none): the larger id decides, not the smaller count.
    expect(chooseM11OverlayHitFeature([anonymous, nested, larger], HIT, counts)).toBe(larger)
    expect(chooseM11OverlayHitFeature([nested, anonymous, larger], HIT, counts)).toBe(larger)
    // Only features without a network id: the map's order.
    const anonymous2 = segment(undefined, 'seg-anon-2')
    expect(chooseM11OverlayHitFeature([anonymous, anonymous2], HIT, counts)).toBe(anonymous)
  })

  it('three networks: the smallest count; one without a count (its id in the middle) switches the counts off', () => {
    const a = segment('rn-a', 'seg-a')
    const b = segment('rn-b', 'seg-b')
    const c = segment('rn-c', 'seg-c')
    const permutations = [[a, b, c], [a, c, b], [b, a, c], [b, c, a], [c, a, b], [c, b, a]]
    for (const order of permutations) {
      expect(chooseM11OverlayHitFeature(order, HIT, { 'rn-a': 300, 'rn-b': 100, 'rn-c': 200 })).toBe(b)
      expect(chooseM11OverlayHitFeature(order, HIT, { 'rn-a': 1, 'rn-c': 2 })).toBe(c)
      expect(chooseM11OverlayHitFeature(order, HIT, null)).toBe(c)
    }
  })

  it('many features of one network keep the map order, also among several networks', () => {
    const b1 = segment('rn-b', 'b1')
    const b2 = segment('rn-b', 'b2')
    const b3 = segment('rn-b', 'b3')
    const c1 = segment('rn-c', 'c1')
    const c2 = segment('rn-c', 'c2')
    expect(chooseM11OverlayHitFeature([b1, b2, b3], HIT, { 'rn-b': 5 })).toBe(b1)
    expect(chooseM11OverlayHitFeature([b3, b1, b2], HIT, null)).toBe(b3)
    const counts = { 'rn-b': 1, 'rn-c': 2 }
    expect(chooseM11OverlayHitFeature([c1, b2, c2, b1], HIT, counts)).toBe(b2)
    expect(chooseM11OverlayHitFeature([b2, c2, b1, c1], HIT, null)).toBe(c2)
    expect(chooseM11OverlayHitFeature([b2, c2, b1, c1], HIT, { 'rn-b': 3, 'rn-c': 3 })).toBe(c2)
  })

  it('compares network ids by UTF-16 code units, not by locale', () => {
    // 'Z' (U+005A) < 'a' (U+0061) by code unit, so descending order puts 'basins_a' first; a
    // locale-aware comparison orders 'a' before 'Z' and would pick 'basins_Z'.
    const upper = segment('basins_Z', 'seg-Z')
    const lower = segment('basins_a', 'seg-a')
    expect('basins_a'.localeCompare('basins_Z')).toBeLessThan(0)
    for (const order of bothOrders(upper, lower)) {
      expect(chooseM11OverlayHitFeature(order, HIT, null)).toBe(lower)
      expect(chooseM11OverlayHitFeature(order, HIT, { basins_Z: 9, basins_a: 9 })).toBe(lower)
    }
  })

  it('does not mutate the input and returns the map object itself', () => {
    const basin = other(M11_BASIN_FILL_LAYER_ID)
    const input = Object.freeze([basin, larger, nested]) as readonly TestFeature[]
    const snapshot = [...input]
    const chosen = chooseM11OverlayHitFeature(input, HIT, { 'rn-b': 7971, 'rn-c': 25971 })
    expect(chosen).toBe(nested)
    expect([...input]).toEqual(snapshot)
    input.forEach((feature, index) => expect(feature).toBe(snapshot[index]))
    expect(nested.properties).toEqual({ river_segment_id: 'seg-b', segment_id: 'seg-b', river_network_version_id: 'rn-b' })
  })
})
