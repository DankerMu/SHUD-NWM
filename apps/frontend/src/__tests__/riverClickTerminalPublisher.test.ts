import { describe, expect, it, vi } from 'vitest'
import { mkdtempSync, rmSync, chmodSync, readFileSync, realpathSync } from 'node:fs'
import { tmpdir } from 'node:os'
import path from 'node:path'

import { publishRiverClickPass, publishRiverClickTerminal } from '../../playwright.river-click-terminal'
import type { RiverClickLaneTerminal } from '../../playwright.river-click-lane'
import { createRiverClickDeadline } from '../lib/riverClickEvidence/deadline'
import { publishRiverClickEvidence } from '../../playwright.river-click-evidence'
import { buildRiverClickPassEvidence, validateRiverClickEvidenceDocument, type RiverClickEvidence } from '../lib/riverClickEvidence/receipt'

const startedAt = '2026-09-02T00:59:50Z'

function passLikeTerminal(overrides: Partial<RiverClickLaneTerminal> = {}): RiverClickLaneTerminal {
  const samples = Array.from({ length: 20 }, (_, i) => ({ index: i + 1, durationMs: 100, gfsStatus: 200, ifsStatus: 200 }))
  return {
    failure: null,
    requestedFeature: { basinId: 'basins_qhh', riverSegmentId: 'seg-001', basinVersionId: 'bv-001', riverNetworkVersionId: 'rn-001' },
    renderedFeature: { basinId: 'basins_qhh', riverSegmentId: 'seg-001', basinVersionId: 'bv-001', riverNetworkVersionId: 'rn-001' },
    gfs: { sourceId: 'GFS', basinId: 'basins_qhh', basinVersionId: 'bv-001', riverNetworkVersionId: 'rn-001', runId: 'run-gfs', modelId: 'model', cycleTime: '2026-09-02T00:00:00Z', scenario: 'forecast_gfs_deterministic' },
    ifs: { sourceId: 'IFS', basinId: 'basins_qhh', basinVersionId: 'bv-001', riverNetworkVersionId: 'rn-001', runId: 'run-ifs', modelId: 'model', cycleTime: '2026-09-02T06:00:00Z', scenario: 'forecast_ifs_deterministic' },
    warmup: { index: 0, durationMs: 200, gfsStatus: 200, ifsStatus: 200 },
    samples,
    p95Ms: 100,
    ...overrides,
  }
}

function blockedTerminal(code: 'HOOK_PREREQUISITE_MISSING' | 'REQUIRED_ENV_MISSING' | 'RUNTIME_UNAVAILABLE', message = 'boom'): RiverClickLaneTerminal {
  return {
    failure: { code, stage: 'runtime', sampleIndex: null, gfsStatus: null, ifsStatus: null, message },
    requestedFeature: { basinId: 'basins_qhh', riverSegmentId: 'seg-001', basinVersionId: 'bv-001', riverNetworkVersionId: 'rn-001' },
    renderedFeature: null,
    gfs: { sourceId: 'GFS', basinId: 'basins_qhh', basinVersionId: 'bv-001', riverNetworkVersionId: 'rn-001', runId: 'run-gfs', modelId: 'model', cycleTime: '2026-09-02T00:00:00Z', scenario: 'forecast_gfs_deterministic' },
    ifs: null,
    warmup: null,
    samples: [],
    p95Ms: null,
  }
}

function failTerminal(code: string, rendered: RiverClickLaneTerminal['renderedFeature'] = null, overrides: Partial<RiverClickLaneTerminal> = {}): RiverClickLaneTerminal {
  const base = passLikeTerminal({ renderedFeature: rendered })
  return {
    ...base,
    failure: {
      code: code as NonNullable<RiverClickLaneTerminal['failure']>['code'],
      stage: code === 'IDENTITY_DRIFT' ? 'sample' : 'map',
      sampleIndex: code === 'IDENTITY_DRIFT' ? 3 : null,
      gfsStatus: null,
      ifsStatus: null,
      message: 'fixture',
    },
    ...overrides,
  }
}

describe('river-click terminal publisher (lane result -> receipt -> publish)', () => {
  it('addInitScript failure (HOOK_PREREQUISITE_MISSING) publishes ONE BLOCKED receipt with null origins/identities/counts, never INTERNAL_ERROR', () => {
    const parent = realpathSync(mkdtempSync(path.join(tmpdir(), 'nhms-term-')))
    try {
      chmodSync(parent, 0o700)
      const receiptPath = path.join(parent, 'nhms-frontend-river-click-live-evidence-terminal.json')
      const terminal = blockedTerminal('HOOK_PREREQUISITE_MISSING')
      const result = publishRiverClickTerminal(terminal, { startedAt, endedAt: '2026-09-02T00:59:51Z', frontendOrigin: 'https://display.example.test', apiOrigin: 'https://api.example.test', receiptPath }, {
        publish: (p, receipt) => publishRiverClickEvidence(p, receipt),
      })
      expect(result.ok).toBe(true)
      const written = JSON.parse(readFileSync(receiptPath, 'utf8'))
      expect(written.status).toBe('BLOCKED')
      expect(written.failure.code).toBe('HOOK_PREREQUISITE_MISSING')
      expect(written.origins.frontend).toBeNull()
      expect(written.origins.api).toBeNull()
      expect(written.requested_feature).toBeNull()
      expect(written.rendered_feature).toBeNull()
      expect(written.gfs).toBeNull()
      expect(written.ifs).toBeNull()
      expect(written.warmup).toBeNull()
      expect(written.samples).toEqual([])
      expect(written.p95_ms).toBeNull()
      expect(validateRiverClickEvidenceDocument(written).ok).toBe(true)
    } finally {
      rmSync(parent, { recursive: true, force: true })
    }
  })

  it('an ordinary partial FAIL preserves known normalized origins and observed partial identities/samples', () => {
    const terminal = failTerminal('SERIES_RESPONSE_ERROR', null, {
      requestedFeature: { basinId: 'basins_qhh', riverSegmentId: 'seg-001', basinVersionId: 'bv-001', riverNetworkVersionId: 'rn-001' },
      renderedFeature: { basinId: 'basins_qhh', riverSegmentId: 'seg-001', basinVersionId: 'bv-001', riverNetworkVersionId: 'rn-001' },
      warmup: { index: 0, durationMs: 300, gfsStatus: 200, ifsStatus: 200 },
      samples: [{ index: 1, durationMs: 500, gfsStatus: 200, ifsStatus: 200 }],
      p95Ms: null,
    })
    const publish = vi.fn(() => ({ path: '/x' }))
    const result = publishRiverClickTerminal(terminal, { startedAt, endedAt: '2026-09-02T00:59:51Z', frontendOrigin: 'https://display.example.test', apiOrigin: 'https://api.example.test', receiptPath: '/x' }, { publish })
    expect(result.ok).toBe(true)
    if (!result.ok) throw new Error(`terminal publication must succeed but returned ${result.code}: ${result.message}`)
    const receipt = result.receipt
    expect(receipt.status).toBe('FAIL')
    if (receipt.failure === null) throw new Error('FAIL receipt must carry a failure')
    expect(receipt.failure.code).toBe('SERIES_RESPONSE_ERROR')
    expect(receipt.origins.frontend).toBe('https://display.example.test')
    expect(receipt.origins.api).toBe('https://api.example.test')
    expect(receipt.requested_feature?.river_segment_id).toBe('seg-001')
    expect(receipt.warmup?.duration_ms).toBe(300)
    expect(receipt.samples).toHaveLength(1)
    expect(validateRiverClickEvidenceDocument(receipt).ok).toBe(true)
  })

  it('lane IDENTITY_DRIFT -> terminal builder -> semantic validator -> publisher retains the actual mismatching rendered identity', () => {
    const terminal = failTerminal('IDENTITY_DRIFT', { basinId: 'basins_qhh', riverSegmentId: 'seg-NEW', basinVersionId: 'bv-001', riverNetworkVersionId: 'rn-001' })
    const publish = vi.fn(() => ({ path: '/x' }))
    const result = publishRiverClickTerminal(terminal, { startedAt, endedAt: '2026-09-02T00:59:51Z', frontendOrigin: 'https://display.example.test', apiOrigin: 'https://api.example.test', receiptPath: '/x' }, { publish })
    expect(result.ok).toBe(true)
    if (!result.ok) throw new Error(`terminal publication must succeed but returned ${result.code}: ${result.message}`)
    const receipt = result.receipt
    expect(receipt.status).toBe('FAIL')
    if (receipt.failure === null) throw new Error('FAIL receipt must carry a failure')
    expect(receipt.failure.code).toBe('IDENTITY_DRIFT')
    // The ACTUAL mismatching rendered identity is retained, never equalized.
    expect(receipt.requested_feature?.river_segment_id).toBe('seg-001')
    expect(receipt.rendered_feature?.river_segment_id).toBe('seg-NEW')
    expect(validateRiverClickEvidenceDocument(receipt).ok).toBe(true)
  })

  it('publication failure is terminal: no second attempt, no old receipt acceptance', () => {
    const terminal = failTerminal('SERIES_RESPONSE_ERROR')
    const publish = vi.fn(() => { throw new Error('publish boom') })
    const result = publishRiverClickTerminal(terminal, { startedAt, endedAt: '2026-09-02T00:59:51Z', frontendOrigin: 'https://display.example.test', apiOrigin: 'https://api.example.test', receiptPath: '/x' }, { publish })
    expect(result.ok).toBe(false)
    if (!result.ok) expect(result.code).toBe('PUBLICATION_FAILED')
    expect(publish).toHaveBeenCalledTimes(1)
  })
})

describe('river-click PASS publisher (whole-deadline-enforced, exactly one publication attempt)', () => {
  function passTerminal(): RiverClickLaneTerminal {
    return passLikeTerminal()
  }
  /** Typed publication mock so mock.calls[0][1] carries the receipt argument. */
  function publishRecording() {
    return vi.fn((_path: string, _receipt: RiverClickEvidence): { path: string } => ({ path: _path }))
  }
  const passInput = (terminal: RiverClickLaneTerminal) => ({
    startedAt,
    endedAt: '2026-09-02T00:59:51Z',
    frontendOrigin: 'https://display.example.test',
    apiOrigin: 'https://api.example.test',
    receiptPath: '/x',
    requestedFeature: terminal.requestedFeature as NonNullable<RiverClickLaneTerminal['requestedFeature']>,
    renderedFeature: terminal.renderedFeature as NonNullable<RiverClickLaneTerminal['renderedFeature']>,
    gfs: terminal.gfs as NonNullable<RiverClickLaneTerminal['gfs']>,
    ifs: terminal.ifs as NonNullable<RiverClickLaneTerminal['ifs']>,
    warmup: terminal.warmup,
    samples: terminal.samples,
  })

  it('PASS builder expiry DURING construction is ok:false WHOLE_RUN_TIMEOUT: publishes exactly ONE FAIL receipt (never a PASS) and never returns success', () => {
    const terminal = passTerminal()
    const publish = publishRecording()
    let elapsed = 0
    // The injected clock expires ONLY during PASS construction: elapsed jumps
    // past the absolute deadline when the (overridden) builder is invoked, and
    // the builder still returns a VALID PASS so the AFTER-expiry path is what
    // decides (a build failure would never reach the AFTER check).
    const deadline = createRiverClickDeadline(100, () => elapsed)
    const result = publishRiverClickPass(terminal, passInput(terminal), { publish }, deadline, {
      build: (input) => {
        elapsed = 200
        const built = buildRiverClickPassEvidence(input)
        return built
      },
    })
    // ok:true means ONLY a PASS receipt was published; a published timeout FAIL
    // is ok:false with the closed WHOLE_RUN_TIMEOUT code and the receipt.
    expect(result.ok).toBe(false)
    if (!result.ok) {
      expect(result.code).toBe('WHOLE_RUN_TIMEOUT')
      expect(result.receipt).not.toBeNull()
      // Exactly one published argument: the FAIL receipt with 1+20 evidence.
      expect(publish).toHaveBeenCalledTimes(1)
      const published = publish.mock.calls[0][1] as { status: string; failure: { code: string; stage: string }; requested_feature: { river_segment_id: string } | null; rendered_feature: { river_segment_id: string } | null; warmup: { index: number } | null; samples: unknown[]; accepted_count: number }
      expect(published.status).toBe('FAIL')
      expect(published.failure.code).toBe('WHOLE_RUN_TIMEOUT')
      expect(published.failure.stage).toBe('sample')
      expect(published.requested_feature?.river_segment_id).toBe('seg-001')
      expect(published.rendered_feature?.river_segment_id).toBe('seg-001')
      expect(published.warmup?.index).toBe(0)
      expect(published.samples).toHaveLength(20)
      expect(published.accepted_count).toBe(20)
      // NO PASS was published: the single published receipt is a FAIL that is
      // still schema-valid (the completed 1+20 evidence is preserved).
      expect(validateRiverClickEvidenceDocument(published).ok).toBe(true)
      // The returned receipt matches the published receipt.
      expect(result.receipt?.status).toBe('FAIL')
      if (result.receipt?.failure === null) throw new Error('FAIL receipt must carry a failure')
      expect(result.receipt?.failure.code).toBe('WHOLE_RUN_TIMEOUT')
    }
  })

  it('expiry BEFORE PASS construction is ok:false WHOLE_RUN_TIMEOUT: publishes exactly ONE FAIL receipt, never a PASS, never success', () => {
    const terminal = passTerminal()
    const publish = publishRecording()
    const result = publishRiverClickPass(terminal, passInput(terminal), { publish }, createRiverClickDeadline(0, () => 5))
    expect(result.ok).toBe(false)
    if (!result.ok) {
      expect(result.code).toBe('WHOLE_RUN_TIMEOUT')
      expect(result.receipt?.status).toBe('FAIL')
      if (result.receipt?.failure === null) throw new Error('FAIL receipt must carry a failure')
      expect(result.receipt?.failure.code).toBe('WHOLE_RUN_TIMEOUT')
    }
    expect(publish).toHaveBeenCalledTimes(1)
    const published = publish.mock.calls[0][1] as { status: string; failure: { code: string } }
    expect(published.status).toBe('FAIL')
    expect(published.failure.code).toBe('WHOLE_RUN_TIMEOUT')
  })

  it('valid PASS publishes exactly one PASS receipt and ok:true; publisher failure is terminal ok:false with receipt null (one attempt, no retry)', () => {
    const terminal = passTerminal()
    const publish = publishRecording()
    const result = publishRiverClickPass(terminal, passInput(terminal), { publish }, null)
    // ok:true AND the published receipt must be PASS (never a non-PASS success).
    expect(result.ok).toBe(true)
    if (!result.ok) throw new Error(`expected PASS but got ${result.code}`)
    expect(result.receipt.status).toBe('PASS')
    expect(publish).toHaveBeenCalledTimes(1)
    const published = publish.mock.calls[0][1] as { status: string }
    expect(published.status).toBe('PASS')
    // Publisher failure: exactly one attempt, no retry, ok:false with no receipt.
    const failing = vi.fn((_path: string, _receipt: RiverClickEvidence): { path: string } => { throw new Error('publish boom') })
    const failed = publishRiverClickPass(terminal, passInput(terminal), { publish: failing }, null)
    expect(failed.ok).toBe(false)
    if (failed.ok) throw new Error('must fail')
    expect(failed.code).toBe('PUBLICATION_FAILED')
    expect(failed.receipt).toBeNull()
    expect(failing).toHaveBeenCalledTimes(1)
  })

  it('caller-seam: the live decision throws on a published WHOLE_RUN_TIMEOUT receipt (nonzero semantics, no republish)', () => {
    // Mimics e2e/live-display.spec.ts: a published timeout receipt makes the
    // caller throw and `publicationAttempted` stays true so the catch cannot
    // republish.
    const terminal = passTerminal()
    let publicationAttempted = false
    const publish = vi.fn((_path: string, receipt: unknown) => {
      publicationAttempted = true
      return { path: '/x' }
    })
    let caught: string | null = null
    const deadline = createRiverClickDeadline(0, () => 5)
    const outcome = publishRiverClickPass(terminal, passInput(terminal), { publish: publish as never }, deadline)
    if (!outcome.ok) {
      caught = `${outcome.code}: ${outcome.message}`
    }
    expect(caught).toMatch(/WHOLE_RUN_TIMEOUT/)
    expect(publish).toHaveBeenCalledTimes(1)
    // The published receipt is a FAIL (never a PASS the runner would accept).
    const published = publish.mock.calls[0][1] as { status: string }
    expect(published.status).toBe('FAIL')
    // The catch guard: publicationAttempted is true -> no second publication.
    let republished = false
    if (!publicationAttempted) republished = true
    expect(republished).toBe(false)
  })
})
