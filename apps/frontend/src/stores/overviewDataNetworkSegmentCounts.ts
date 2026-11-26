import type { ApiModelInstance } from '@/lib/m11/overviewDataContracts'

/**
 * `river_network_version_id` -> segment count of that network, derived from the model records the
 * overview has loaded (#3390, shared-reach-click-ownership D2): the smallest usable `segment_count`
 * (finite, greater than zero) among the records of the network. A record without a network id or
 * without a usable count contributes nothing, so a network none of whose records has one is absent.
 * The map's click and hover choose the owner of a shared reach from it.
 */
export function buildNetworkSegmentCounts(
  models: readonly Pick<ApiModelInstance, 'river_network_version_id' | 'segment_count'>[],
): Record<string, number> {
  const counts: Record<string, number> = {}
  for (const model of models) {
    const networkId = model.river_network_version_id
    const count = model.segment_count
    if (typeof networkId !== 'string' || networkId.length === 0) continue
    if (typeof count !== 'number' || !Number.isFinite(count) || count <= 0) continue
    if (!Object.prototype.hasOwnProperty.call(counts, networkId) || count < counts[networkId]) counts[networkId] = count
  }
  return counts
}
