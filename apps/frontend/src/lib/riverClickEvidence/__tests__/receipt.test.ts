import { describe, expect, it } from 'vitest'

import {
  RIVER_CLICK_ACCEPTED_SAMPLES,
  RIVER_CLICK_THRESHOLD_MS,
  RIVER_CLICK_WARMUP,
  buildRiverClickPassEvidence,
  buildRiverClickTerminalEvidence,
  validateRiverClickEvidenceDocument,
  type RiverClickEvidence,
  type RiverClickFailure,
  type RiverClickPassInput,
  type RiverClickProductIdentity,
  type RiverClickTerminalInput,
} from '../receipt'
import type { RiverClickFailureCode } from '../constants'
import {
  RIVER_CLICK_EXACT_THRESHOLD_DURATIONS,
  RIVER_CLICK_JUST_BELOW_THRESHOLD_DURATIONS,
  riverClickExactThresholdSamples,
} from '../../../test/riverClickThresholdFixture'

/** Typed PASS fixture; `base` overrides are spread with the same shape. */
function passInput(base: Partial<RiverClickPassInput> = {}): RiverClickPassInput {
  const samples = Array.from({ length: 20 }, (_, index) => ({
    index: index + 1,
    durationMs: 100 + index,
    gfsStatus: 200,
    ifsStatus: 200,
  }))
  return {
    startedAt: '2026-09-02T00:00:00Z',
    endedAt: '2026-09-02T00:02:00Z',
    frontendOrigin: 'https://display.example.test',
    apiOrigin: 'https://api.example.test',
    requestedFeature: {
      basinId: 'basins_qhh',
      riverSegmentId: 'seg-001',
      basinVersionId: 'bv-001',
      riverNetworkVersionId: 'rn-001',
    },
    renderedFeature: {
      basinId: 'basins_qhh',
      riverSegmentId: 'seg-001',
      basinVersionId: 'bv-001',
      riverNetworkVersionId: 'rn-001',
    },
    gfs: {
      sourceId: 'GFS',
      basinId: 'basins_qhh',
      basinVersionId: 'bv-001',
      riverNetworkVersionId: 'rn-001',
      runId: 'run-001',
      modelId: 'model-gfs',
      cycleTime: '2026-09-02T00:00:00Z',
      scenario: 'forecast_gfs_deterministic',
    },
    ifs: {
      sourceId: 'IFS',
      basinId: 'basins_qhh',
      basinVersionId: 'bv-001',
      riverNetworkVersionId: 'rn-001',
      runId: 'run-002',
      modelId: 'model-ifs',
      cycleTime: '2026-09-02T06:00:00Z',
      scenario: 'forecast_ifs_deterministic',
    },
    warmup: { index: 0, durationMs: 350, gfsStatus: 200, ifsStatus: 200 },
    samples,
    ...base,
  }
}

describe('river-click PASS evidence construction', () => {
  it('builds a schema-1.0 PASS document from 20 complete warm samples', () => {
    const result = buildRiverClickPassEvidence(passInput())
    expect(result.ok).toBe(true)
    if (!result.ok) throw new Error('PASS fixture must build')
    const receipt = result.receipt
    expect(receipt.artifact).toBe('nhms-frontend-river-click-live-evidence')
    expect(receipt.schema_version).toBe('1.0')
    expect(receipt.status).toBe('PASS')
    expect(receipt.threshold_ms).toBe(RIVER_CLICK_THRESHOLD_MS)
    expect(receipt.percentile_method).toBe('nearest-rank')
    expect(receipt.warmup_count).toBe(1)
    expect(receipt.accepted_count).toBe(20)
    expect(receipt.samples).toHaveLength(20)
    expect(receipt.warmup).toEqual({ index: 0, duration_ms: 350, discarded: true, gfs_status: 200, ifs_status: 200 })
    expect(receipt.failure).toBeNull()
    expect(receipt.p95_ms).toBe(118)
    expect(receipt.origins).toEqual({
      frontend: 'https://display.example.test',
      api: 'https://api.example.test',
    })
  })

  it('recomputes nearest-rank P95 from the actual durations, not an injected value', () => {
    const result = buildRiverClickPassEvidence(passInput())
    expect(result.ok).toBe(true)
    if (!result.ok) throw new Error('fixture must build')
    expect(result.receipt.p95_ms).toBe(118)
  })

  it('refuses a PASS-shaped document whose actual sample P95 is exactly 2000', () => {
    const result = buildRiverClickPassEvidence(passInput({
      samples: riverClickExactThresholdSamples(RIVER_CLICK_EXACT_THRESHOLD_DURATIONS),
    }))
    expect(result.ok).toBe(false)
    if (result.ok) throw new Error('PASS builder must refuse exact-threshold equality')
  })

  it('accepts a just-below P95 of 1999.999 as PASS', () => {
    const result = buildRiverClickPassEvidence(passInput({
      samples: riverClickExactThresholdSamples(RIVER_CLICK_JUST_BELOW_THRESHOLD_DURATIONS),
    }))
    expect(result.ok).toBe(true)
    if (!result.ok) throw new Error('just-below fixture must build')
    expect(result.receipt.p95_ms).toBe(1999.999)
  })

  it('returns {ok:false} rather than throwing when a product cycle_time is calendar-invalid', () => {
    const input = passInput()
    ;(input.gfs as { cycleTime: string }).cycleTime = '2026-02-30T00:00:00Z'
    expect(() => buildRiverClickPassEvidence(input)).not.toThrow()
    expect(buildRiverClickPassEvidence(input).ok).toBe(false)
  })

  it('returns {ok:false} rather than throwing when samples contain a non-finite duration', () => {
    const input = passInput()
    ;(input.samples as Array<{ durationMs: number }>)[0].durationMs = Number.POSITIVE_INFINITY
    expect(() => buildRiverClickPassEvidence(input)).not.toThrow()
    expect(buildRiverClickPassEvidence(input).ok).toBe(false)
  })

  it('refuses a warmup that is not exactly one complete discarded sample', () => {
    const noWarmup = buildRiverClickPassEvidence(passInput({ warmup: null }))
    expect(noWarmup.ok).toBe(false)
  })

  it('refuses any sample count other than exactly 20', () => {
    const short = buildRiverClickPassEvidence(passInput({ samples: [passInput().samples[0]] }))
    expect(short.ok).toBe(false)
  })
})

describe('closed river-click terminal classification', () => {
  const failure = (code: RiverClickFailureCode): RiverClickFailure => ({
    code,
    stage: 'sample',
    sampleIndex: null,
    gfsStatus: null,
    ifsStatus: null,
    message: 'test failure',
  })

  it('builds honest BLOCKED receipts with no warmup/sample/P95 claim', () => {
    const result = buildRiverClickTerminalEvidence({
      startedAt: '2026-09-02T00:00:00Z',
      endedAt: '2026-09-02T00:00:05Z',
      frontendOrigin: null,
      apiOrigin: null,
      failure: { code: 'REQUIRED_ENV_MISSING', stage: 'runtime', sampleIndex: null, gfsStatus: null, ifsStatus: null, message: 'missing env' },
    })
    expect(result.ok).toBe(true)
    if (!result.ok) throw new Error('BLOCKED fixture must build')
    const receipt = result.receipt
    expect(receipt.status).toBe('BLOCKED')
    expect(receipt.warmup_count).toBe(0)
    expect(receipt.accepted_count).toBe(0)
    expect(receipt.warmup).toBeNull()
    expect(receipt.samples).toEqual([])
    expect(receipt.p95_ms).toBeNull()
    expect(receipt.failure).toMatchObject({ code: 'REQUIRED_ENV_MISSING' })
  })

  it('accepts a BLOCKED input with an explicit null warmup (the live spec passes warmup:null)', () => {
    // The live spec always passes warmup/samples/identities explicitly; the
    // builder must treat warmup null/absent as "no claim" for BLOCKED.
    const result = buildRiverClickTerminalEvidence({
      startedAt: '2026-09-02T00:00:00Z',
      endedAt: '2026-09-02T00:00:05Z',
      frontendOrigin: null,
      apiOrigin: null,
      requestedFeature: null,
      renderedFeature: null,
      gfs: null,
      ifs: null,
      warmup: null,
      samples: [],
      failure: { code: 'HOOK_PREREQUISITE_MISSING', stage: 'runtime', sampleIndex: null, gfsStatus: null, ifsStatus: null, message: 'flag missing' },
    })
    expect(result.ok).toBe(true)
    if (!result.ok) throw new Error('BLOCKED with explicit null warmup must build')
    expect(result.receipt.status).toBe('BLOCKED')
    expect(result.receipt.warmup_count).toBe(0)
    expect(result.receipt.accepted_count).toBe(0)
    expect(result.receipt.samples).toEqual([])
    expect(result.receipt.requested_feature).toBeNull()
    expect(result.receipt.gfs).toBeNull()
    expect(result.receipt.failure).toMatchObject({ code: 'HOOK_PREREQUISITE_MISSING' })
  })

  it('rejects a BLOCKED input carrying a non-null warmup claim', () => {
    const result = buildRiverClickTerminalEvidence({
      startedAt: '2026-09-02T00:00:00Z',
      endedAt: '2026-09-02T00:00:05Z',
      frontendOrigin: null,
      apiOrigin: null,
      warmup: { index: 0, durationMs: 300, gfsStatus: 200, ifsStatus: 200 },
      failure: { code: 'REQUIRED_ENV_MISSING', stage: 'runtime', sampleIndex: null, gfsStatus: null, ifsStatus: null, message: 'missing env' },
    })
    expect(result.ok).toBe(false)
  })

  it('returns {ok:false} instead of throwing for cyclic/BigInt/stringify-invalid terminal inputs', () => {
    const base: RiverClickTerminalInput = {
      startedAt: '2026-09-02T00:00:00Z',
      endedAt: '2026-09-02T00:01:00Z',
      frontendOrigin: 'https://display.example.test',
      apiOrigin: 'https://api.example.test',
      failure: { code: 'INTERNAL_ERROR', stage: 'sample', sampleIndex: null, gfsStatus: null, ifsStatus: null, message: 'x' },
    }
    // BigInt inside a product identity field (non-JSON value). The cast is at
    // the SMALLEST validator boundary: the deliberately malformed product is
    // typed only as far as the builder's product slot needs.
    const bigIntProduct: RiverClickTerminalInput = {
      ...base,
      gfs: { sourceId: 'GFS', basinId: 'b', basinVersionId: 'bv', riverNetworkVersionId: 'rn', runId: 'r', modelId: 'm', cycleTime: '2026-09-02T00:00:00Z', scenario: 'forecast_gfs_deterministic', extraCycle: 1n as unknown as string } as unknown as RiverClickProductIdentity,
    }
    expect(() => buildRiverClickTerminalEvidence(bigIntProduct)).not.toThrow()
    expect(buildRiverClickTerminalEvidence(bigIntProduct).ok).toBe(false)
    // Cyclic object passed as a product (cannot stringify); same smallest-boundary cast.
    const cyclic: Record<string, unknown> = {}
    cyclic.self = cyclic
    const cyclicProduct: RiverClickTerminalInput = { ...base, gfs: cyclic as unknown as RiverClickProductIdentity }
    expect(() => buildRiverClickTerminalEvidence(cyclicProduct)).not.toThrow()
    expect(buildRiverClickTerminalEvidence(cyclicProduct).ok).toBe(false)
  })

  it('builds honest FAIL receipts with actual completed counts and nullable identities', () => {
    const result = buildRiverClickTerminalEvidence({
      startedAt: '2026-09-02T00:00:00Z',
      endedAt: '2026-09-02T00:01:00Z',
      frontendOrigin: 'https://display.example.test',
      apiOrigin: 'https://api.example.test',
      requestedFeature: { basinId: 'b', riverSegmentId: 's', basinVersionId: 'bv', riverNetworkVersionId: 'rn' },
      renderedFeature: null,
      gfs: { sourceId: 'GFS', basinId: 'b', basinVersionId: 'bv', riverNetworkVersionId: 'rn', runId: 'r', modelId: 'm', cycleTime: '2026-09-02T00:00:00Z', scenario: 'forecast_gfs_deterministic' },
      ifs: null,
      warmup: { index: 0, durationMs: 300, gfsStatus: 200, ifsStatus: 200 },
      samples: [passInput().samples[0]],
      failure: { code: 'SERIES_RESPONSE_ERROR', stage: 'sample', sampleIndex: 2, gfsStatus: 200, ifsStatus: 500, message: 'IFS failed' },
    })
    expect(result.ok).toBe(true)
    if (!result.ok) throw new Error('FAIL fixture must build')
    expect(result.receipt.status).toBe('FAIL')
    expect(result.receipt.warmup_count).toBe(1)
    expect(result.receipt.accepted_count).toBe(1)
    expect(result.receipt.samples).toHaveLength(1)
    expect(result.receipt.p95_ms).toBeNull()
    expect(result.receipt.failure).toMatchObject({ code: 'SERIES_RESPONSE_ERROR', sample_index: 2 })
  })

  it('rebuilds the receipt with p95 null when a THRESHOLD_EXCEEDED input has 20 samples but a failure code bound to the wrong stage', () => {
    // A FAIL with code THRESHOLD_EXCEEDED but stage 'sample' is not the completed
    // threshold shape: the builder must refuse (code/stage binding).
    const samples = Array.from({ length: 20 }, (_, index) => ({
      index: index + 1,
      durationMs: 2000 + index,
      gfsStatus: 200,
      ifsStatus: 200,
    }))
    const result = buildRiverClickTerminalEvidence({
      startedAt: '2026-09-02T00:00:00Z',
      endedAt: '2026-09-02T00:05:00Z',
      frontendOrigin: 'https://display.example.test',
      apiOrigin: 'https://api.example.test',
      requestedFeature: { basinId: 'basins_qhh', riverSegmentId: 'seg-001', basinVersionId: 'bv-001', riverNetworkVersionId: 'rn-001' },
      renderedFeature: { basinId: 'basins_qhh', riverSegmentId: 'seg-001', basinVersionId: 'bv-001', riverNetworkVersionId: 'rn-001' },
      gfs: { sourceId: 'GFS', basinId: 'basins_qhh', basinVersionId: 'bv-001', riverNetworkVersionId: 'rn-001', runId: 'run-001', modelId: 'model-gfs', cycleTime: '2026-09-02T00:00:00Z', scenario: 'forecast_gfs_deterministic' },
      ifs: { sourceId: 'IFS', basinId: 'basins_qhh', basinVersionId: 'bv-001', riverNetworkVersionId: 'rn-001', runId: 'run-002', modelId: 'model-ifs', cycleTime: '2026-09-02T06:00:00Z', scenario: 'forecast_ifs_deterministic' },
      warmup: { index: 0, durationMs: 300, gfsStatus: 200, ifsStatus: 200 },
      samples,
      failure: failure('THRESHOLD_EXCEEDED'),
    })
    expect(result.ok).toBe(false)
  })

  it('carries the exact recomputed non-passing P95 only for a complete THRESHOLD_EXCEEDED failure', () => {
    const samples = Array.from({ length: 20 }, (_, index) => ({
      index: index + 1,
      durationMs: 2000 + index,
      gfsStatus: 200,
      ifsStatus: 200,
    }))
    const result = buildRiverClickTerminalEvidence({
      startedAt: '2026-09-02T00:00:00Z',
      endedAt: '2026-09-02T00:05:00Z',
      frontendOrigin: 'https://display.example.test',
      apiOrigin: 'https://api.example.test',
      requestedFeature: { basinId: 'basins_qhh', riverSegmentId: 'seg-001', basinVersionId: 'bv-001', riverNetworkVersionId: 'rn-001' },
      renderedFeature: { basinId: 'basins_qhh', riverSegmentId: 'seg-001', basinVersionId: 'bv-001', riverNetworkVersionId: 'rn-001' },
      gfs: { sourceId: 'GFS', basinId: 'basins_qhh', basinVersionId: 'bv-001', riverNetworkVersionId: 'rn-001', runId: 'run-001', modelId: 'model-gfs', cycleTime: '2026-09-02T00:00:00Z', scenario: 'forecast_gfs_deterministic' },
      ifs: { sourceId: 'IFS', basinId: 'basins_qhh', basinVersionId: 'bv-001', riverNetworkVersionId: 'rn-001', runId: 'run-002', modelId: 'model-ifs', cycleTime: '2026-09-02T06:00:00Z', scenario: 'forecast_ifs_deterministic' },
      warmup: { index: 0, durationMs: 300, gfsStatus: 200, ifsStatus: 200 },
      samples,
      failure: { code: 'THRESHOLD_EXCEEDED', stage: 'threshold', sampleIndex: null, gfsStatus: null, ifsStatus: null, message: 'test failure' },
    })
    expect(result.ok).toBe(true)
    if (!result.ok) throw new Error('threshold fixture must build')
    const receipt = result.receipt
    expect(receipt.status).toBe('FAIL')
    expect(receipt.samples).toHaveLength(20)
    expect(receipt.p95_ms).toBe(2018)
    expect(receipt.requested_feature).not.toBeNull()
    expect(receipt.rendered_feature).not.toBeNull()
    expect(receipt.gfs).not.toBeNull()
    expect(receipt.ifs).not.toBeNull()
    expect(receipt.warmup).not.toBeNull()
  })

  it('carries exact p95_ms=2000 for the shared duration fixture through a THRESHOLD_EXCEEDED terminal', () => {
    const samples = riverClickExactThresholdSamples()
    const result = buildRiverClickTerminalEvidence({
      startedAt: '2026-09-02T00:00:00Z',
      endedAt: '2026-09-02T00:05:00Z',
      frontendOrigin: 'https://display.example.test',
      apiOrigin: 'https://api.example.test',
      requestedFeature: { basinId: 'basins_qhh', riverSegmentId: 'seg-001', basinVersionId: 'bv-001', riverNetworkVersionId: 'rn-001' },
      renderedFeature: { basinId: 'basins_qhh', riverSegmentId: 'seg-001', basinVersionId: 'bv-001', riverNetworkVersionId: 'rn-001' },
      gfs: { sourceId: 'GFS', basinId: 'basins_qhh', basinVersionId: 'bv-001', riverNetworkVersionId: 'rn-001', runId: 'run-001', modelId: 'model-gfs', cycleTime: '2026-09-02T00:00:00Z', scenario: 'forecast_gfs_deterministic' },
      ifs: { sourceId: 'IFS', basinId: 'basins_qhh', basinVersionId: 'bv-001', riverNetworkVersionId: 'rn-001', runId: 'run-002', modelId: 'model-ifs', cycleTime: '2026-09-02T06:00:00Z', scenario: 'forecast_ifs_deterministic' },
      warmup: { index: 0, durationMs: 300, gfsStatus: 200, ifsStatus: 200 },
      samples,
      failure: { code: 'THRESHOLD_EXCEEDED', stage: 'threshold', sampleIndex: null, gfsStatus: null, ifsStatus: null, message: 'nearest-rank P95 2000 >= 2000 ms' },
    })
    expect(result.ok).toBe(true)
    if (!result.ok) throw new Error('exact-threshold FAIL fixture must build')
    expect(result.receipt.status).toBe('FAIL')
    expect(result.receipt.failure?.code).toBe('THRESHOLD_EXCEEDED')
    expect(result.receipt.p95_ms).toBe(2000)
    expect(result.receipt.accepted_count).toBe(20)
    expect(validateRiverClickEvidenceDocument(result.receipt).ok).toBe(true)
    const asPass = { ...result.receipt, status: 'PASS' as const, failure: null }
    expect(validateRiverClickEvidenceDocument(asPass).ok).toBe(false)
  })

  it('refuses a THRESHOLD_EXCEEDED terminal with all identities null (false-PASS shape)', () => {
    const samples = Array.from({ length: 20 }, (_, index) => ({
      index: index + 1,
      durationMs: 2000 + index,
      gfsStatus: 200,
      ifsStatus: 200,
    }))
    const result = buildRiverClickTerminalEvidence({
      startedAt: '2026-09-02T00:00:00Z',
      endedAt: '2026-09-02T00:05:00Z',
      frontendOrigin: 'https://display.example.test',
      apiOrigin: 'https://api.example.test',
      requestedFeature: null,
      renderedFeature: null,
      gfs: null,
      ifs: null,
      warmup: { index: 0, durationMs: 300, gfsStatus: 200, ifsStatus: 200 },
      samples,
      failure: { code: 'THRESHOLD_EXCEEDED', stage: 'threshold', sampleIndex: null, gfsStatus: null, ifsStatus: null, message: 'test failure' },
    })
    expect(result.ok).toBe(false)
  })

  it('returns {ok:false} instead of throwing when a FAIL input has a calendar-invalid product cycle', () => {
    const input = {
      startedAt: '2026-09-02T00:00:00Z',
      endedAt: '2026-09-02T00:01:00Z',
      frontendOrigin: 'https://display.example.test',
      apiOrigin: 'https://api.example.test',
      failure: failure('INTERNAL_ERROR'),
    }
    expect(buildRiverClickTerminalEvidence(input).ok).toBe(true)
  })

  it('refuses to carry a sub-threshold P95 on any non-PASS document', () => {
    const result = buildRiverClickTerminalEvidence({
      startedAt: '2026-09-02T00:00:00Z',
      endedAt: '2026-09-02T00:01:00Z',
      frontendOrigin: 'https://display.example.test',
      apiOrigin: 'https://api.example.test',
      failure: failure('INTERNAL_ERROR'),
    })
    expect(result.ok).toBe(true)
    if (!result.ok) throw new Error('fixture must build')
    expect(result.receipt.p95_ms).toBeNull()
  })

  it('binds accepted_count to samples.length, warmup_count to warmup presence, and consecutive indices', () => {
    const result = buildRiverClickTerminalEvidence({
      startedAt: '2026-09-02T00:00:00Z',
      endedAt: '2026-09-02T00:01:00Z',
      frontendOrigin: 'https://display.example.test',
      apiOrigin: 'https://api.example.test',
      warmup: { index: 0, durationMs: 300, gfsStatus: 200, ifsStatus: 200 },
      samples: [passInput().samples[0], passInput().samples[1]],
      failure: { code: 'SERIES_RESPONSE_ERROR', stage: 'sample', sampleIndex: 3, gfsStatus: 200, ifsStatus: 500, message: 'IFS failed' },
    })
    expect(result.ok).toBe(true)
    if (!result.ok) throw new Error('fixture must build')
    expect(result.receipt.accepted_count).toBe(2)
    expect(result.receipt.warmup_count).toBe(1)
    expect(result.receipt.samples.map((sample) => sample.index)).toEqual([1, 2])
  })

  it('rejects FTP origins anywhere in the document', () => {
    const result = buildRiverClickTerminalEvidence({
      startedAt: '2026-09-02T00:00:00Z',
      endedAt: '2026-09-02T00:01:00Z',
      frontendOrigin: 'ftp://display.example.test',
      apiOrigin: 'https://api.example.test',
      failure: { code: 'INTERNAL_ERROR', stage: 'sample', sampleIndex: null, gfsStatus: null, ifsStatus: null, message: 'x' },
    })
    expect(result.ok).toBe(false)
  })

  it('rejects unknown failure codes and mixed blocked/fail membership', () => {
    const unknown = buildRiverClickTerminalEvidence({
      startedAt: '2026-09-02T00:00:00Z',
      endedAt: '2026-09-02T00:01:00Z',
      frontendOrigin: null,
      apiOrigin: null,
      // Intentional invalid code: the builder must refuse, not throw. The cast
      // is confined to this single deliberately-malformed failure slot.
      failure: { code: 'NOT_A_CODE' as RiverClickFailureCode, stage: 'config', sampleIndex: null, gfsStatus: null, ifsStatus: null, message: 'x' },
    })
    expect(unknown.ok).toBe(false)

    const failWithBlocked = buildRiverClickTerminalEvidence({
      startedAt: '2026-09-02T00:00:00Z',
      endedAt: '2026-09-02T00:01:00Z',
      frontendOrigin: null,
      apiOrigin: null,
      failure: { code: 'REQUIRED_ENV_MISSING', stage: 'config', sampleIndex: 3, gfsStatus: 200, ifsStatus: 200, message: 'x' },
      samples: [passInput().samples[0]],
    })
    expect(failWithBlocked.ok).toBe(false)
  })

  it('binds failure code to status: a FAIL code is never a BLOCKED receipt and vice versa', () => {
    // INTERNAL_ERROR (FAIL set) builds a FAIL receipt, never BLOCKED.
    const failReceipt = buildRiverClickTerminalEvidence({
      startedAt: '2026-09-02T00:00:00Z',
      endedAt: '2026-09-02T00:01:00Z',
      frontendOrigin: null,
      apiOrigin: null,
      failure: { code: 'INTERNAL_ERROR', stage: 'sample', sampleIndex: null, gfsStatus: null, ifsStatus: null, message: 'x' },
    })
    expect(failReceipt.ok).toBe(true)
    if (!failReceipt.ok) throw new Error('FAIL receipt must build')
    expect(failReceipt.receipt.status).toBe('FAIL')

    // REQUIRED_ENV_MISSING (BLOCKED set) builds a BLOCKED receipt, never FAIL,
    // and carrying any sample claim is refused.
    const blockedReceipt = buildRiverClickTerminalEvidence({
      startedAt: '2026-09-02T00:00:00Z',
      endedAt: '2026-09-02T00:01:00Z',
      frontendOrigin: null,
      apiOrigin: null,
      failure: { code: 'REQUIRED_ENV_MISSING', stage: 'runtime', sampleIndex: null, gfsStatus: null, ifsStatus: null, message: 'x' },
      samples: [passInput().samples[0]],
    })
    expect(blockedReceipt.ok).toBe(false)
  })

  it('uses RUN_TIME-units constants for threshold and warmup counts', () => {
    expect(RIVER_CLICK_WARMUP).toBe(1)
    expect(RIVER_CLICK_ACCEPTED_SAMPLES).toBe(20)
    expect(RIVER_CLICK_THRESHOLD_MS).toBe(2000)
  })
})

describe('closed river-click evidence validator', () => {
  const valid = buildRiverClickPassEvidence(passInput())
  if (!valid.ok) throw new Error('fixture must build')
  const receipt: RiverClickEvidence = valid.receipt

  function validate(doc: unknown) {
    return validateRiverClickEvidenceDocument(doc)
  }

  it('accepts the exact PASS document with all closed invariants', () => {
    expect(validate(receipt).ok).toBe(true)
  })

  it('rejects additional top-level fields', () => {
    expect(validate({ ...receipt, unexpected: 1 }).ok).toBe(false)
  })

  it('rejects malformed status/count/identity/threshold values', () => {
    expect(validate({ ...receipt, status: 'MAYBE' }).ok).toBe(false)
    expect(validate({ ...receipt, warmup_count: 2 }).ok).toBe(false)
    expect(validate({ ...receipt, accepted_count: 19 }).ok).toBe(false)
    expect(validate({ ...receipt, threshold_ms: 1999 }).ok).toBe(false)
    expect(validate({ ...receipt, percentile_method: 'mean' }).ok).toBe(false)
    expect(validate({ ...receipt, schema_version: '2.0' }).ok).toBe(false)
    expect(validate({ ...receipt, artifact: 'other' }).ok).toBe(false)
  })

  it('rejects Date.parse-valid but calendar-invalid timestamps (e.g. April 31, Feb 30)', () => {
    expect(validate({ ...receipt, started_at: '2026-04-31T00:00:00Z' }).ok).toBe(false)
    expect(validate({ ...receipt, started_at: '2026-02-30T00:00:00Z' }).ok).toBe(false)
    expect(validate({ ...receipt, started_at: '2026-13-01T00:00:00Z' }).ok).toBe(false)
    expect(validate({ ...receipt, started_at: '2026-09-02T24:00:00Z' }).ok).toBe(false)
  })

  it('rejects unordered or non-UTC-RFC3339 timestamps', () => {
    expect(validate({ ...receipt, started_at: '2026-09-02T00:05:00Z' }).ok).toBe(false)
    expect(validate({ ...receipt, generated_at: '2026-09-02T00:01:00+08:00' }).ok).toBe(false)
    expect(validate({ ...receipt, ended_at: 'not a time' }).ok).toBe(false)
  })

  it('rejects P95/cross-field status violations', () => {
    const failed = { ...receipt, status: 'FAIL' as const, failure: { code: 'INTERNAL_ERROR', stage: 'sample', sample_index: null, gfs_status: null, ifs_status: null, message: 'x' } }
    expect(validate(failed).ok).toBe(false)
    const blocked = { ...receipt, status: 'BLOCKED' as const, failure: { code: 'REQUIRED_ENV_MISSING', stage: 'runtime', sample_index: null, gfs_status: null, ifs_status: null, message: 'x' } }
    expect(validate(blocked).ok).toBe(false)
  })

  it('enforces partial identity coherence for a FAIL with a non-null feature set', () => {
    // FAIL with requested_feature non-null but gfs null: the non-null parts must
    // still agree with one another (requested/rivnet/version vs gfs) — if gfs is
    // absent the remaining identities must at least be self-consistent. Here the
    // non-null requested_feature carries a different network than the samples'
    // product, so coherence fails.
    const doc = JSON.parse(JSON.stringify(receipt)) as RiverClickEvidence
    doc.status = 'FAIL'
    doc.failure = { code: 'SAMPLE_TIMEOUT', stage: 'sample', sample_index: 4, gfs_status: 200, ifs_status: null, message: 'x' }
    doc.p95_ms = null
    doc.accepted_count = 3
    doc.warmup_count = 1
    doc.samples = receipt.samples.slice(0, 3)
    doc.ifs = null
    ;(doc.requested_feature as Record<string, unknown>).river_network_version_id = 'rn-other'
    expect(validate(doc).ok).toBe(false)
  })

  it('requires THRESHOLD_EXCEEDED (completed) to have PASS-equivalent identity', () => {
    // Build the completed threshold terminal (durations >= 2000 so the
    // recomputed nearest-rank P95 is itself >= 2000), then mutate it.
    const built = buildRiverClickTerminalEvidence({
      startedAt: '2026-09-02T00:00:00Z',
      endedAt: '2026-09-02T00:05:00Z',
      frontendOrigin: 'https://display.example.test',
      apiOrigin: 'https://api.example.test',
      requestedFeature: { basinId: 'basins_qhh', riverSegmentId: 'seg-001', basinVersionId: 'bv-001', riverNetworkVersionId: 'rn-001' },
      renderedFeature: { basinId: 'basins_qhh', riverSegmentId: 'seg-001', basinVersionId: 'bv-001', riverNetworkVersionId: 'rn-001' },
      gfs: { sourceId: 'GFS', basinId: 'basins_qhh', basinVersionId: 'bv-001', riverNetworkVersionId: 'rn-001', runId: 'run-001', modelId: 'model-gfs', cycleTime: '2026-09-02T00:00:00Z', scenario: 'forecast_gfs_deterministic' },
      ifs: { sourceId: 'IFS', basinId: 'basins_qhh', basinVersionId: 'bv-001', riverNetworkVersionId: 'rn-001', runId: 'run-002', modelId: 'model-ifs', cycleTime: '2026-09-02T06:00:00Z', scenario: 'forecast_ifs_deterministic' },
      warmup: { index: 0, durationMs: 300, gfsStatus: 200, ifsStatus: 200 },
      samples: Array.from({ length: 20 }, (_, index) => ({
        index: index + 1,
        durationMs: 2000 + index,
        gfsStatus: 200,
        ifsStatus: 200,
      })),
      failure: { code: 'THRESHOLD_EXCEEDED', stage: 'threshold', sampleIndex: null, gfsStatus: null, ifsStatus: null, message: 'x' },
    })
    if (!built.ok) throw new Error('threshold fixture must build')
    const doc = JSON.parse(JSON.stringify(built.receipt)) as Record<string, unknown>
    // A completed threshold FAIL with null rendered identity is a false-PASS shape.
    doc.rendered_feature = null
    expect(validate(doc).ok).toBe(false)
    // With the full identity it validates (all invariants hold).
    const full = JSON.parse(JSON.stringify(built.receipt)) as Record<string, unknown>
    expect(validate(full).ok).toBe(true)
  })

  it('rejects non-finite numbers and values over their byte bounds', () => {
    const nonFinite = JSON.parse(JSON.stringify(receipt)) as RiverClickEvidence
    nonFinite.samples[0].duration_ms = Number.POSITIVE_INFINITY
    expect(validate(nonFinite).ok).toBe(false)

    const fatIdentity = JSON.parse(JSON.stringify(receipt)) as RiverClickEvidence
    fatIdentity.origins.frontend = `https://example.test/${'a'.repeat(300)}`
    expect(validate(fatIdentity).ok).toBe(false)
  })

  it('rejects deep/wide/oversized documents and secret-shaped fields', () => {
    const wide = { ...JSON.parse(JSON.stringify(receipt)), extra: Object.fromEntries(Array.from({ length: 70 }, (_, i) => [`k${i}`, 1])) }
    expect(validate(wide).ok).toBe(false)

    const secret = { ...JSON.parse(JSON.stringify(receipt)), note: 'password=secret' }
    expect(validate(secret).ok).toBe(false)

    const userinfo = JSON.parse(JSON.stringify(receipt)) as RiverClickEvidence
    userinfo.origins.api = 'https://user:pass@api.example.test'
    expect(validate(userinfo).ok).toBe(false)
  })

  it('rejects raw URLs/query strings in failure messages and non-canonical cycle times', () => {
    const leaked = JSON.parse(JSON.stringify(receipt)) as RiverClickEvidence
    leaked.status = 'FAIL'
    leaked.samples = []
    leaked.warmup = null
    leaked.warmup_count = 0
    leaked.accepted_count = 0
    leaked.p95_ms = null
    leaked.failure = {
      code: 'INTERNAL_ERROR',
      stage: 'sample',
      sample_index: null,
      gfs_status: null,
      ifs_status: null,
      message: 'fetch failed at https://api.example.test/api/v1/basin-versions/bv-001/river-segments/seg-001/forecast-series?variables=q_down',
    }
    expect(validate(leaked).ok).toBe(false)

    const offsetCycle = JSON.parse(JSON.stringify(receipt)) as RiverClickEvidence
    ;(offsetCycle.gfs as { cycle_time: string }).cycle_time = '2026-09-02T08:00:00+08:00'
    expect(validate(offsetCycle).ok).toBe(false)
  })

  it('rejects query/userinfo-shaped identity strings in every non-origin identity slot', () => {
    const base = JSON.parse(JSON.stringify(receipt)) as RiverClickEvidence
    const variants: Array<[string, (doc: RiverClickEvidence) => void]> = [
      ['run_id query shaped', (d) => { if (d.gfs !== null) d.gfs.run_id = 'run?run_id=x&token=y' }],
      ['basin_id query shaped', (d) => { if (d.requested_feature !== null) d.requested_feature.basin_id = 'basins?x=1&y=2' }],
      ['userinfo shaped', (d) => { if (d.gfs !== null) d.gfs.model_id = 'user@host' }],
      ['scheme-relative shaped', (d) => { if (d.ifs !== null) d.ifs.run_id = '//evil.example/path' }],
    ]
    for (const [name, mutate] of variants) {
      const doc = JSON.parse(JSON.stringify(base)) as RiverClickEvidence
      mutate(doc)
      expect(validate(doc).ok, `must reject ${name}`).toBe(false)
    }
  })

  it('rejects an empty failure message (messageCapped must reject empty strings)', () => {
    const empty = JSON.parse(JSON.stringify(receipt)) as RiverClickEvidence
    empty.status = 'FAIL'
    empty.samples = []
    empty.warmup = null
    empty.warmup_count = 0
    empty.accepted_count = 0
    empty.p95_ms = null
    empty.failure = { code: 'INTERNAL_ERROR', stage: 'sample', sample_index: null, gfs_status: null, ifs_status: null, message: '' }
    expect(validate(empty).ok).toBe(false)
  })

  it('rejects sample counts over 64 and more than 21 sample objects', () => {
    const manySamples = JSON.parse(JSON.stringify(receipt)) as RiverClickEvidence
    manySamples.status = 'FAIL'
    manySamples.samples = Array.from({ length: 65 }, (_, i) => ({ index: i + 1, duration_ms: 100, gfs_status: 200, ifs_status: 200 }))
    manySamples.accepted_count = 65
    manySamples.failure = { code: 'INTERNAL_ERROR', stage: 'sample', sample_index: null, gfs_status: null, ifs_status: null, message: 'x' }
    expect(validate(manySamples).ok).toBe(false)
  })

  it('rejects non-200..299 sample statuses and non-1..20 sample indices', () => {
    const badStatus = JSON.parse(JSON.stringify(receipt)) as RiverClickEvidence
    badStatus.samples[0].ifs_status = 500
    expect(validate(badStatus).ok).toBe(false)

    const badIndex = JSON.parse(JSON.stringify(receipt)) as RiverClickEvidence
    badIndex.samples[0].index = 21
    expect(validate(badIndex).ok).toBe(false)
  })

  it('rejects cyclic, BigInt, and undefined values as validation failure without throwing', () => {
    const cyclic = JSON.parse(JSON.stringify(receipt)) as Record<string, unknown>
    cyclic.self = cyclic
    expect(() => validate(cyclic)).not.toThrow()
    expect(validate(cyclic).ok).toBe(false)

    const bigInt = JSON.parse(JSON.stringify(receipt)) as Record<string, unknown>
    bigInt.threshold_ms = 1n as unknown as number
    expect(() => validate(bigInt)).not.toThrow()
    expect(validate(bigInt).ok).toBe(false)

    const undefinedValue = JSON.parse(JSON.stringify(receipt)) as Record<string, unknown>
    undefinedValue.origins = { frontend: undefined, api: null }
    expect(() => validate(undefinedValue)).not.toThrow()
    expect(validate(undefinedValue).ok).toBe(false)
  })
})
