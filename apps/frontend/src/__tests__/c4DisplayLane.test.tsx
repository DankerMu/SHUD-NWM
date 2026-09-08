import { describe, expect, it, vi } from 'vitest'
import { Script, createContext } from 'node:vm'

import { render, screen } from '@testing-library/react'

import { JobsTable } from '@/components/monitoring/JobsTable'
import { useMonitoringStore } from '@/stores/monitoring'
import { parseC4DisplayConfig } from '../lib/c4DisplayEvidence/config'
import {
  inspectC4HomeInPage,
  inspectC4OpsInPage,
  openC4JobLogInPage,
  type C4HomeDomObservation,
  type C4OpsDomObservation,
} from '../lib/c4DisplayEvidence/dom'
import { extractC4JobIdFromJobsPayload, matchC4OpsRequest } from '../lib/c4DisplayEvidence/requestMatching'
import { runC4DisplayLane } from '../../playwright.c4-display-lane'
import { mapRiverFailure } from '../../playwright.c4-display-lane-preflight'
import { C4_PER_STEP_DEADLINE_MS, C4_QUIET_PERIOD_MS, C4_WHOLE_RUN_DEADLINE_MS } from '../lib/c4DisplayEvidence/constants'
import { createRiverClickDeadline } from '../lib/riverClickEvidence/deadline'
import {
  defaultC4HomeObservation,
  defaultC4OpsObservation,
  installC4HomeDom,
  installC4OpsDom,
  makeC4FakePage,
  makeC4FakePageState,
  type C4FakeResponseSpec,
} from '../test/c4DisplayFakePage'

const CONFIG = {
  PLAYWRIGHT_LIVE_BASE_URL: 'https://display.example.test',
  PLAYWRIGHT_LIVE_API_BASE_URL: 'https://api.example.test',
  PLAYWRIGHT_LIVE_C4_BASIN_ID: 'basins_qhh',
  PLAYWRIGHT_LIVE_C4_SEGMENT_ID: 'seg-001',
  PLAYWRIGHT_LIVE_C4_RECEIPT_PATH: '/private/evidence/nhms-frontend-c4-live-evidence-1.json',
}

function config() {
  const parsed = parseC4DisplayConfig(CONFIG)
  if (!parsed.ok) throw new Error('fixture config must parse')
  return parsed.config
}

function productPayload(source: 'GFS' | 'IFS') {
  return {
    basin_id: 'basins_qhh',
    model_id: source === 'GFS' ? 'model-gfs' : 'model-ifs',
    basin_version_id: 'bv-001',
    river_network_version_id: 'rn-001',
    source_id: source,
    cycle_time: source === 'GFS' ? '2026-09-04T00:00:00Z' : '2026-09-04T06:00:00Z',
    run_id: `run-${source.toLowerCase()}`,
    status: 'ready',
    availability: { ready: true },
  }
}

const SEGMENT_PAYLOAD = {
  river_segment_id: 'seg-001',
  river_network_version_id: 'rn-001',
  geom: { type: 'LineString', coordinates: [[100, 30], [101, 31], [102, 32]] },
}

function envelope(data: unknown) {
  return { status: 'ok', data }
}

function jsonResponse(payload: unknown, status = 200): Response {
  return new Response(JSON.stringify(payload), {
    status,
    headers: { 'content-type': 'application/json', 'content-encoding': 'identity' },
  })
}

function defaultFetch() {
  return vi.fn(async (url: string) => {
    const parsed = new URL(url)
    if (parsed.pathname === '/api/v1/mvp/qhh/latest-product') {
      const source = parsed.searchParams.get('source') === 'IFS' ? 'IFS' : 'GFS'
      return jsonResponse(envelope(productPayload(source)))
    }
    if (parsed.pathname.includes('/river-segments/')) return jsonResponse(envelope(SEGMENT_PAYLOAD))
    throw new Error(`no fake route for ${parsed.pathname}`)
  })
}

function identity(source: 'GFS' | 'IFS') {
  const product = productPayload(source)
  return {
    sourceId: source,
    basinId: product.basin_id,
    basinVersionId: product.basin_version_id,
    riverNetworkVersionId: product.river_network_version_id,
    runId: product.run_id,
    modelId: product.model_id,
    cycleTime: source === 'GFS' ? '2026-09-04T00:00:00.000Z' : '2026-09-04T06:00:00.000Z',
    scenario: source === 'GFS' ? 'forecast_gfs_deterministic' : 'forecast_ifs_deterministic',
  } as const
}

function opsResponses(source: 'GFS' | 'IFS'): C4FakeResponseSpec[] {
  const product = identity(source)
  const query = `source=${product.sourceId}&cycle_time=${encodeURIComponent(product.cycleTime)}&run_id=${product.runId}&model_id=${product.modelId}`
  const api = 'https://api.example.test'
  return [
    { url: `${api}/api/v1/pipeline/status?${query}`, status: 200, body: envelope({ source: product.sourceId, cycle_time: product.cycleTime }) },
    { url: `${api}/api/v1/pipeline/stages?${query}`, status: 200, body: envelope([]) },
    {
      url: `${api}/api/v1/jobs?${query}`,
      status: 200,
      body: envelope({ items: [{ job_id: `job-${source.toLowerCase()}`, run_id: product.runId, model_id: product.modelId }] }),
    },
  ]
}

function homeResponses(): C4FakeResponseSpec[] {
  return [
    {
      url: 'https://api.example.test/api/v1/runtime/config',
      status: 200,
      body: { data: { service_role: 'display_readonly', display_readonly: true } },
    },
    { url: 'https://api.example.test/api/v1/basins?limit=200', status: 200, body: envelope({ items: [] }) },
  ]
}

function logResponses(jobId: string, opsUrl: string): C4FakeResponseSpec[] {
  const source = opsUrl.includes('IFS') ? 'IFS' : 'GFS'
  const product = identity(source)
  const query = `source=${product.sourceId}&cycle_time=${encodeURIComponent(product.cycleTime)}&run_id=${product.runId}&model_id=${product.modelId}`
  return [{ url: `https://api.example.test/api/v1/jobs/${jobId}/logs?${query}`, status: 200, body: envelope({ job_id: jobId, content: 'ok' }) }]
}

describe('C4 request matching and job selection', () => {
  it('redacts upstream preflight error text before C4 terminal publication', () => {
    const failure = mapRiverFailure({
      code: 'PREFLIGHT_HTTP_ERROR',
      stage: 'preflight',
      sampleIndex: null,
      gfsStatus: null,
      ifsStatus: null,
      message: 'file:///private/secret ws://socket.example.test user@example.test',
    })
    expect(failure.message).toBe('C4 preflight request failed')
  })

  it('matches strict ops query names and refuses the loose cycle param', () => {
    const gfs = identity('GFS')
    const api = 'https://api.example.test'
    const query = `source=GFS&cycle_time=${encodeURIComponent(gfs.cycleTime)}&run_id=${gfs.runId}&model_id=${gfs.modelId}`
    expect(matchC4OpsRequest('GET', `${api}/api/v1/pipeline/status?${query}`, api, gfs).matched).toBe(true)
    expect(matchC4OpsRequest('GET', `${api}/api/v1/pipeline/status?source=GFS&cycle=${encodeURIComponent(gfs.cycleTime)}&run_id=${gfs.runId}&model_id=${gfs.modelId}`, api, gfs).matched).toBe(false)
    expect(matchC4OpsRequest('GET', `${api}/api/v1/jobs/${encodeURIComponent('job-gfs')}/logs?${query}`, api, gfs)).toEqual({
      matched: true,
      kind: 'logs',
      jobId: 'job-gfs',
    })
  })

  it('selects the first API-ordered current-identity job and refuses a wrong-source job', () => {
    const gfs = identity('GFS')
    expect(extractC4JobIdFromJobsPayload(envelope({
      items: [{ job_id: 'job-gfs', run_id: gfs.runId, model_id: gfs.modelId }],
    }), gfs)).toBe('job-gfs')
    expect(extractC4JobIdFromJobsPayload(envelope({
      items: [{ job_id: 'job-ifs', run_id: 'run-ifs', model_id: 'model-ifs' }],
    }), gfs)).toBeNull()
    expect(extractC4JobIdFromJobsPayload(envelope({
      items: [
        { job_id: 'job-gfs-a', run_id: gfs.runId, model_id: gfs.modelId },
        { job_id: 'job-gfs-b', run_id: gfs.runId, model_id: gfs.modelId },
      ],
    }), gfs)).toBe('job-gfs-a')
    expect(extractC4JobIdFromJobsPayload(envelope({
      items: [
        { job_id: 'job-gfs-a', run_id: gfs.runId, model_id: gfs.modelId },
        { job_id: 'job-gfs-a', run_id: gfs.runId, model_id: gfs.modelId },
      ],
    }), gfs)).toBeNull()
    expect(extractC4JobIdFromJobsPayload(envelope({ items: [] }), gfs)).toBeNull()
  })
})

describe('C4 production DOM helpers', () => {
  it('inspects home and ops from the actual DOM, not a synthesized success', () => {
    installC4HomeDom(defaultC4HomeObservation())
    expect(inspectC4HomeInPage()).toMatchObject({ mapPresent: true, mapWidth: 800, loading: false })
    installC4OpsDom(defaultC4OpsObservation(), { jobId: 'job-gfs' })
    expect(inspectC4OpsInPage()).toMatchObject({ headingObserved: true, permissionDenied: false })
    expect(openC4JobLogInPage('job-gfs')).toEqual({ clicked: true })
    expect(openC4JobLogInPage('missing')).toEqual({ clicked: false })
  })

  it('opens the shipping JobsTable log UI through the production helper', () => {
    document.body.innerHTML = ''
    useMonitoringStore.setState({
      jobs: [{
        job_id: 'job-gfs',
        run_id: 'run-gfs',
        cycle_id: 'cycle-1',
        run_type: 'forecast',
        scenario: 'forecast_gfs_deterministic',
        job_type: 'forecast',
        slurm_job_id: '1001',
        model_id: 'model-gfs',
        status: 'succeeded',
        stage: 'forecast',
        submitted_at: '2026-09-04T00:03:00Z',
        started_at: '2026-09-04T00:04:00Z',
        finished_at: '2026-09-04T00:06:00Z',
        exit_code: 0,
        retry_count: 0,
        error_code: null,
        error_message: null,
        log_uri: 's3://logs/job-gfs.log',
        duration_seconds: 120,
      }],
      jobTotal: 1,
      jobsError: null,
      isJobsLoading: false,
      fetchJobs: vi.fn().mockResolvedValue(undefined),
    })
    render(
      <JobsTable
        diagnosticsEnabled={true}
        diagnosticsDisplayReadonly={true}
        logControlsEnabled={true}
        retryControlsEnabled={false}
        cancelControlsEnabled={false}
        fetchEnabled={true}
        displayEnabled={true}
        autoFetch={false}
      />,
    )
    expect(screen.getByText('作业列表')).toBeVisible()
    expect(screen.getByText('job-gfs')).toBeVisible()
    expect(openC4JobLogInPage('job-gfs')).toEqual({ clicked: true })
    expect(openC4JobLogInPage('job-missing')).toEqual({ clicked: false })
  })
})

describe('C4 live lane', () => {
  it('passes home plus both strict /ops identities and detaches listeners', async () => {
    const state = makeC4FakePageState({
      homeResponses: homeResponses(),
      opsResponsesFor: (url) => opsResponses(url.includes('IFS') ? 'IFS' : 'GFS'),
      logResponsesFor: (jobId, opsUrl) => {
        const source = opsUrl.includes('IFS') ? 'IFS' : 'GFS'
        const product = identity(source)
        const query = `source=${product.sourceId}&cycle_time=${encodeURIComponent(product.cycleTime)}&run_id=${product.runId}&model_id=${product.modelId}`
        return [{ url: `https://api.example.test/api/v1/jobs/${jobId}/logs?${query}`, status: 200, body: envelope({ job_id: jobId, content: 'ok' }) }]
      },
    })
    const page = makeC4FakePage(state)
    const result = await runC4DisplayLane({ config: config(), page }, defaultFetch())
    expect(result.ok).toBe(true)
    if (!result.ok) throw new Error('PASS lane must succeed')
    expect(result.terminal.home?.currentReadPath).toBe('/api/v1/basins')
    expect(result.terminal.opsGfs?.jobId).toBe('job-gfs')
    expect(result.terminal.opsIfs?.jobId).toBe('job-ifs')
    expect(state.gotoUrls[0]).toBe('/')
    expect(state.gotoUrls[1]).toContain('/ops?source=GFS&cycle_time=')
    expect(state.gotoUrls[2]).toContain('/ops?source=IFS&cycle_time=')
    expect(state.gotoUrls.join()).not.toContain('cycle=')
    expect(state.listeners.request).toEqual([])
    expect(state.listeners.response).toEqual([])
  })

  it('fails permission denied without spoofing role', async () => {
    const state = makeC4FakePageState({
      homeResponses: homeResponses(),
      opsObservation: { ...defaultC4OpsObservation(), headingObserved: false, permissionDenied: true },
      opsResponsesFor: (url) => opsResponses(url.includes('IFS') ? 'IFS' : 'GFS'),
      logResponsesFor: () => [],
    })
    const page = makeC4FakePage(state)
    const result = await runC4DisplayLane({ config: config(), page }, defaultFetch())
    expect(result.ok).toBe(false)
    if (result.ok) throw new Error('denied ops must fail')
    expect(result.terminal.failure?.code).toBe('PERMISSION_DENIED')
  })

  it('fails missing jobs, wrong-source jobs, and forbidden slurm control', async () => {
    const missingJobs = makeC4FakePageState({
      homeResponses: homeResponses(),
      opsResponsesFor: (url) => opsResponses(url.includes('IFS') ? 'IFS' : 'GFS').map((spec) => (
        spec.url.includes('/api/v1/jobs?') ? { ...spec, body: envelope({ items: [] }) } : spec
      )),
      logResponsesFor: () => [],
    })
    const missing = await runC4DisplayLane({ config: config(), page: makeC4FakePage(missingJobs) }, defaultFetch())
    expect(missing.ok).toBe(false)
    if (missing.ok) throw new Error('empty jobs must fail')
    expect(missing.terminal.failure?.code).toBe('JOB_LOG_MISSING')

    const forbidden = makeC4FakePageState({
      homeResponses: homeResponses(),
      extraRequestsOnGoto: (url) => url.includes('/ops')
        ? [{ method: 'GET', url: 'https://api.example.test/api/v1/slurm/jobs' }]
        : [],
      opsResponsesFor: (url) => opsResponses(url.includes('IFS') ? 'IFS' : 'GFS'),
      logResponsesFor: () => [],
    })
    const forbiddenResult = await runC4DisplayLane({ config: config(), page: makeC4FakePage(forbidden) }, defaultFetch())
    expect(forbiddenResult.ok).toBe(false)
    if (forbiddenResult.ok) throw new Error('slurm must fail')
    expect(forbiddenResult.terminal.failure?.code).toBe('FORBIDDEN_CONTROL')
  })

  it('fails GFS/IFS identity mismatch at preflight', async () => {
    const fetchImpl = vi.fn(async (url: string) => {
      const parsed = new URL(url)
      if (parsed.pathname === '/api/v1/mvp/qhh/latest-product') {
        const source = parsed.searchParams.get('source') === 'IFS' ? 'IFS' : 'GFS'
        const payload = productPayload(source)
        if (source === 'IFS') payload.basin_version_id = 'bv-other'
        return jsonResponse(envelope(payload))
      }
      if (parsed.pathname.includes('/river-segments/')) return jsonResponse(envelope(SEGMENT_PAYLOAD))
      throw new Error(`no fake route for ${parsed.pathname}`)
    })
    const result = await runC4DisplayLane({ config: config(), page: makeC4FakePage(makeC4FakePageState()) }, fetchImpl)
    expect(result.ok).toBe(false)
    if (result.ok) throw new Error('mismatch must fail')
    expect(result.terminal.failure?.code).toBe('IDENTITY_MISMATCH')
  })

  it('waits through home loading until the map is ready, including the quiet recheck', async () => {
    const ready = defaultC4HomeObservation()
    const state = makeC4FakePageState({
      homeResponses: homeResponses(),
      homeObservations: [
        { ...ready, loading: true },
        ready,
        { ...ready, loading: true },
        ready,
      ],
      opsResponsesFor: (url) => opsResponses(url.includes('IFS') ? 'IFS' : 'GFS'),
      logResponsesFor: (jobId, opsUrl) => {
        const source = opsUrl.includes('IFS') ? 'IFS' : 'GFS'
        const product = identity(source)
        const query = `source=${product.sourceId}&cycle_time=${encodeURIComponent(product.cycleTime)}&run_id=${product.runId}&model_id=${product.modelId}`
        return [{ url: `https://api.example.test/api/v1/jobs/${jobId}/logs?${query}`, status: 200, body: envelope({ job_id: jobId, content: 'ok' }) }]
      },
    })

    const result = await runC4DisplayLane({ config: config(), page: makeC4FakePage(state) }, defaultFetch())

    expect(result.ok).toBe(true)
  })

  it('fails home confirmation when its second valid observation arrives after the step deadline', async () => {
    const clock = { now: 0 }
    const deadline = createRiverClickDeadline(C4_WHOLE_RUN_DEADLINE_MS, () => clock.now)
    const state = makeC4FakePageState({
      homeResponses: homeResponses(),
      opsResponsesFor: (url) => opsResponses(url.includes('IFS') ? 'IFS' : 'GFS'),
      logResponsesFor: logResponses,
    })
    const page = makeC4FakePage(state)
    const originalWait = page.waitForTimeout.bind(page)
    const originalEvaluate = page.evaluate.bind(page)
    let quietFinishedWithinStep = false
    let delayedConfirmationWasValid = false
    let afterQuiet = false
    page.waitForTimeout = (async (ms: number) => {
      await originalWait(ms)
      clock.now += ms
      if (ms === C4_QUIET_PERIOD_MS && state.gotoUrls.at(-1) === '/') {
        quietFinishedWithinStep = clock.now < C4_PER_STEP_DEADLINE_MS
        afterQuiet = true
      }
    }) as typeof page.waitForTimeout
    page.evaluate = (async (expression: unknown, ...args: unknown[]) => {
      const observed = await originalEvaluate(expression as never, ...args)
      if (expression === inspectC4HomeInPage && afterQuiet) {
        delayedConfirmationWasValid = (observed as C4HomeDomObservation).mapPresent
        clock.now += C4_PER_STEP_DEADLINE_MS
        afterQuiet = false
      }
      return observed
    }) as typeof page.evaluate

    const result = await runC4DisplayLane({ config: config(), page }, defaultFetch(), { deadline })

    expect(quietFinishedWithinStep).toBe(true)
    expect(delayedConfirmationWasValid).toBe(true)
    expect(clock.now).toBeLessThan(C4_WHOLE_RUN_DEADLINE_MS)
    expect(result.ok).toBe(false)
    if (result.ok) throw new Error('late home confirmation must fail')
    expect(result.terminal.failure?.code).toBe('STEP_TIMEOUT')
  })

  it('fails ops confirmation when its second valid observation arrives after the step deadline', async () => {
    const clock = { now: 0 }
    const deadline = createRiverClickDeadline(C4_WHOLE_RUN_DEADLINE_MS, () => clock.now)
    const state = makeC4FakePageState({
      homeResponses: homeResponses(),
      opsResponsesFor: (url) => opsResponses(url.includes('IFS') ? 'IFS' : 'GFS'),
      logResponsesFor: logResponses,
    })
    const page = makeC4FakePage(state)
    const originalWait = page.waitForTimeout.bind(page)
    const originalEvaluate = page.evaluate.bind(page)
    let quietFinishedWithinStep = false
    let delayedConfirmationWasValid = false
    let afterQuiet = false
    page.waitForTimeout = (async (ms: number) => {
      await originalWait(ms)
      clock.now += ms
      if (ms === C4_QUIET_PERIOD_MS && state.gotoUrls.at(-1)?.includes('/ops')) {
        quietFinishedWithinStep = clock.now < C4_PER_STEP_DEADLINE_MS
        afterQuiet = true
      }
    }) as typeof page.waitForTimeout
    page.evaluate = (async (expression: unknown, ...args: unknown[]) => {
      const observed = await originalEvaluate(expression as never, ...args)
      if (expression === inspectC4OpsInPage && afterQuiet) {
        delayedConfirmationWasValid = (observed as C4OpsDomObservation).headingObserved
        clock.now += C4_PER_STEP_DEADLINE_MS
        afterQuiet = false
      }
      return observed
    }) as typeof page.evaluate

    const result = await runC4DisplayLane({ config: config(), page }, defaultFetch(), { deadline })

    expect(quietFinishedWithinStep).toBe(true)
    expect(delayedConfirmationWasValid).toBe(true)
    expect(clock.now).toBeLessThan(C4_WHOLE_RUN_DEADLINE_MS)
    expect(result.ok).toBe(false)
    if (result.ok) throw new Error('late ops confirmation must fail')
    expect(result.terminal.failure?.code).toBe('STEP_TIMEOUT')
  })

  it('times out rather than classifying a permanently loading home as not-ready', async () => {
    const clock = { now: 0 }
    const deadline = createRiverClickDeadline(25, () => clock.now)
    const state = makeC4FakePageState({
      homeResponses: homeResponses(),
      homeObservation: { ...defaultC4HomeObservation(), loading: true },
      homeObservations: Array.from({ length: 32 }, () => ({ ...defaultC4HomeObservation(), loading: true })),
      sleepMs: 0,
    })
    const page = makeC4FakePage(state)
    page.waitForTimeout = async (ms) => { clock.now += ms }

    const result = await runC4DisplayLane({ config: config(), page }, defaultFetch(), { deadline })

    expect(result.ok).toBe(false)
    if (result.ok) throw new Error('permanently loading home must time out')
    expect(result.terminal.failure?.code).toBe('STEP_TIMEOUT')
  })

  it('fails home map-unavailable, visible source errors, and whole-run timeout', async () => {
    const unavailable = makeC4FakePageState({
      homeObservation: { ...defaultC4HomeObservation(), mapUnavailable: true },
      homeResponses: homeResponses(),
    })
    const homeFail = await runC4DisplayLane({ config: config(), page: makeC4FakePage(unavailable) }, defaultFetch())
    expect(homeFail.ok).toBe(false)
    if (homeFail.ok) throw new Error('unavailable map must fail')
    expect(homeFail.terminal.failure?.code).toBe('HOME_NOT_READY')

    const sourceError = makeC4FakePageState({
      homeObservation: { ...defaultC4HomeObservation(), mapSourceError: true },
      homeResponses: homeResponses(),
    })
    const sourceErrorResult = await runC4DisplayLane({ config: config(), page: makeC4FakePage(sourceError) }, defaultFetch())
    expect(sourceErrorResult.ok).toBe(false)
    if (sourceErrorResult.ok) throw new Error('visible map source error must fail')
    expect(sourceErrorResult.terminal.failure?.code).toBe('HOME_NOT_READY')

    const clock = { now: 0 }
    const deadline = createRiverClickDeadline(1, () => clock.now)
    clock.now = 2
    const timeout = await runC4DisplayLane({ config: config(), page: makeC4FakePage(makeC4FakePageState()) }, defaultFetch(), { deadline })
    expect(timeout.ok).toBe(false)
    if (timeout.ok) throw new Error('expired deadline must fail')
    expect(timeout.terminal.failure?.code).toBe('WHOLE_RUN_TIMEOUT')
  })

  it('classifies an unexpected page failure without publishing raw error text', async () => {
    const result = await runC4DisplayLane(
      { config: config(), page: makeC4FakePage(makeC4FakePageState()) },
      async () => { throw new Error('file:///private/secret') },
    )
    expect(result.ok).toBe(false)
    if (result.ok) throw new Error('unexpected page failure must not pass')
    expect(result.terminal.failure).toEqual({
      code: 'PREFLIGHT_HTTP_ERROR',
      stage: 'preflight',
      message: 'C4 preflight request failed',
    })
  })

  it('accepts missing content-length for bounded runtime config and jobs bodies', async () => {
    const withoutContentLength = (spec: C4FakeResponseSpec): C4FakeResponseSpec => ({ ...spec, contentLength: null })
    const state = makeC4FakePageState({
      homeResponses: homeResponses().map(withoutContentLength),
      opsResponsesFor: (url) => opsResponses(url.includes('IFS') ? 'IFS' : 'GFS').map(withoutContentLength),
      logResponsesFor: (jobId, opsUrl) => {
        const source = opsUrl.includes('IFS') ? 'IFS' : 'GFS'
        const product = identity(source)
        const query = `source=${product.sourceId}&cycle_time=${encodeURIComponent(product.cycleTime)}&run_id=${product.runId}&model_id=${product.modelId}`
        return [withoutContentLength({
          url: `https://api.example.test/api/v1/jobs/${jobId}/logs?${query}`,
          status: 200,
          body: envelope({ job_id: jobId, content: 'ok' }),
        })]
      },
    })

    const result = await runC4DisplayLane({ config: config(), page: makeC4FakePage(state) }, defaultFetch())

    expect(result.ok).toBe(true)
  })

  it('rejects declared and actual oversized bodies without accepting a receipt body', async () => {
    const declaredTextCalls = { value: 0 }
    const actualTextCalls = { value: 0 }
    const oversized = '界'.repeat(87_382)
    const expiredDeadline = () => {
      const clock = { now: 0 }
      const deadline = createRiverClickDeadline(25, () => clock.now)
      return { clock, deadline }
    }
    const declared = makeC4FakePageState({
      homeResponses: [{
        ...homeResponses()[0],
        contentLength: '262145',
        textCallCount: declaredTextCalls,
      }, homeResponses()[1]],
      sleepMs: 0,
    })
    const declaredClock = expiredDeadline()
    const declaredPage = makeC4FakePage(declared)
    declaredPage.waitForTimeout = async (ms) => { declaredClock.clock.now += ms }
    const declaredResult = await runC4DisplayLane({ config: config(), page: declaredPage }, defaultFetch(), { deadline: declaredClock.deadline })
    expect(declaredResult.ok).toBe(false)
    expect(declaredTextCalls.value).toBe(0)

    const actual = makeC4FakePageState({
      homeResponses: [{
        ...homeResponses()[0],
        contentLength: null,
        text: async () => oversized,
        textCallCount: actualTextCalls,
      }, homeResponses()[1]],
      sleepMs: 0,
    })
    const actualClock = expiredDeadline()
    const actualPage = makeC4FakePage(actual)
    actualPage.waitForTimeout = async (ms) => { actualClock.clock.now += ms }
    const actualResult = await runC4DisplayLane({ config: config(), page: actualPage }, defaultFetch(), { deadline: actualClock.deadline })
    expect(actualResult.ok).toBe(false)
    expect(actualTextCalls.value).toBe(1)
  })

  it('does not accept headers-only, failed completion, or stalled jobs bodies', async () => {
    const stalled = new Promise<string>(() => undefined)
    const cases: Array<{ name: string; response: Partial<C4FakeResponseSpec> }> = [
      { name: 'headers-only status', response: { finished: () => new Promise(() => undefined) } },
      { name: 'post-header logs failure', response: { finished: () => Promise.resolve(new Error('network failed')) } },
      { name: 'stalled jobs body', response: { text: () => stalled } },
    ]
    for (const { name, response } of cases) {
      const state = makeC4FakePageState({
        homeResponses: homeResponses(),
        opsResponsesFor: (url) => opsResponses(url.includes('IFS') ? 'IFS' : 'GFS').map((spec) => {
          if (name === 'headers-only status' && spec.url.includes('/pipeline/status?')) return { ...spec, ...response }
          if (name === 'stalled jobs body' && spec.url.includes('/api/v1/jobs?')) return { ...spec, ...response }
          return spec
        }),
        logResponsesFor: (jobId, opsUrl) => {
          const source = opsUrl.includes('IFS') ? 'IFS' : 'GFS'
          const product = identity(source)
          const query = `source=${product.sourceId}&cycle_time=${encodeURIComponent(product.cycleTime)}&run_id=${product.runId}&model_id=${product.modelId}`
          return [{
            url: `https://api.example.test/api/v1/jobs/${jobId}/logs?${query}`,
            status: 200,
            body: envelope({ job_id: jobId, content: 'ok' }),
            ...(name === 'post-header logs failure' ? response : {}),
          }]
        },
      })
      const deadline = createRiverClickDeadline(25)
      const result = await runC4DisplayLane({ config: config(), page: makeC4FakePage(state) }, defaultFetch(), { deadline })
      expect(result.ok, name).toBe(false)
      if (!result.ok) {
        expect(['STEP_TIMEOUT', 'OPS_UNAVAILABLE', 'JOB_LOG_MISSING'], name).toContain(result.terminal.failure?.code)
      }
    }
  })

  it('ignores aborted non-required API and tile requests while transitioning to ops', async () => {
    const state = makeC4FakePageState({
      homeResponses: homeResponses(),
      failRequests: [
        {
          url: 'https://api.example.test/api/v1/models',
          status: 0,
          failure: { errorText: 'net::ERR_ABORTED' },
        },
        {
          url: 'https://tiles.example.test/tiles/river/1/2/3.pbf',
          status: 0,
          failure: { errorText: 'net::ERR_ABORTED' },
        },
      ],
      opsResponsesFor: (url) => opsResponses(url.includes('IFS') ? 'IFS' : 'GFS'),
      logResponsesFor: (jobId, opsUrl) => {
        const source = opsUrl.includes('IFS') ? 'IFS' : 'GFS'
        const product = identity(source)
        const query = `source=${product.sourceId}&cycle_time=${encodeURIComponent(product.cycleTime)}&run_id=${product.runId}&model_id=${product.modelId}`
        return [{ url: `https://api.example.test/api/v1/jobs/${jobId}/logs?${query}`, status: 200, body: envelope({ job_id: jobId, content: 'ok' }) }]
      },
    })

    const result = await runC4DisplayLane({ config: config(), page: makeC4FakePage(state) }, defaultFetch())

    expect(result.ok).toBe(true)
  })

  it('fails home with RUNTIME_CONFIG_INVALID for a complete 200 contradictory runtime body', async () => {
    const state = makeC4FakePageState({
      homeResponses: [
        {
          url: 'https://api.example.test/api/v1/runtime/config',
          status: 200,
          body: { data: { service_role: 'display_readonly', display_readonly: false } },
        },
        homeResponses()[1],
      ],
    })

    const result = await runC4DisplayLane({ config: config(), page: makeC4FakePage(state) }, defaultFetch())

    expect(result.ok).toBe(false)
    if (result.ok) throw new Error('contradictory runtime config must fail')
    expect(result.terminal.failure?.code).toBe('RUNTIME_CONFIG_INVALID')
  })

  it('fails home with RUNTIME_CONFIG_INVALID when runtime config completionError is set', async () => {
    const state = makeC4FakePageState({
      homeResponses: [
        {
          ...homeResponses()[0],
          finished: () => Promise.resolve(new Error('runtime config body failed')),
        },
        homeResponses()[1],
      ],
    })

    const result = await runC4DisplayLane({ config: config(), page: makeC4FakePage(state) }, defaultFetch())

    expect(result.ok).toBe(false)
    if (result.ok) throw new Error('runtime completionError must fail')
    expect(result.terminal.failure?.code).toBe('RUNTIME_CONFIG_INVALID')
  })

  it.each([
    ['home runtime config', 'https://api.example.test/api/v1/runtime/config', 'HOME_NOT_READY'],
    ['home current read', 'https://api.example.test/api/v1/basins?limit=200', 'HOME_NOT_READY'],
    ['ops status', 'https://api.example.test/api/v1/pipeline/status?source=GFS&cycle_time=2026-09-04T00%3A00%3A00.000Z&run_id=run-gfs&model_id=model-gfs', 'OPS_UNAVAILABLE'],
  ] as const)('fails when required %s request has a non-abort network failure', async (_name, url, expectedCode) => {
    const state = makeC4FakePageState({
      homeResponses: homeResponses(),
      failRequests: [{ url, status: 0, failure: { errorText: 'net::ERR_CONNECTION_RESET' } }],
      opsResponsesFor: (opsUrl) => opsResponses(opsUrl.includes('IFS') ? 'IFS' : 'GFS'),
      logResponsesFor: () => [],
    })

    const result = await runC4DisplayLane({ config: config(), page: makeC4FakePage(state) }, defaultFetch())

    expect(result.ok).toBe(false)
    if (result.ok) throw new Error('required request failure must fail')
    expect(result.terminal.failure?.code).toBe(expectedCode)
  })

  it('counts every target-origin non-GET request and rejects it', async () => {
    const state = makeC4FakePageState({
      homeResponses: homeResponses(),
      extraRequestsOnGoto: (url) => url.includes('/ops')
        ? [{ method: 'POST', url: 'https://api.example.test/api/v1/models/model-gfs/lifecycle' }]
        : [],
      opsResponsesFor: (url) => opsResponses(url.includes('IFS') ? 'IFS' : 'GFS'),
      logResponsesFor: () => [],
    })
    const result = await runC4DisplayLane({ config: config(), page: makeC4FakePage(state) }, defaultFetch())
    expect(result.ok).toBe(false)
    if (result.ok) throw new Error('POST must fail')
    expect(result.terminal.failure?.code).toBe('FORBIDDEN_CONTROL')
    expect(result.terminal.noControl).toEqual({ slurmRequestCount: 0, nonGetControlCount: 1 })
  })

  it('runs helper source in an isolated DOM realm without module closures', () => {
    installC4OpsDom(defaultC4OpsObservation(), { jobId: 'job-gfs' })
    const realm = createContext({ document, Array, RegExp })
    const evaluateSerialized = (fn: Function, ...args: unknown[]): unknown => {
      realm.args = args
      new Script(`globalThis.result = (${fn.toString()})(...args)`).runInContext(realm)
      return realm.result
    }

    expect(evaluateSerialized(inspectC4OpsInPage)).toMatchObject({
      headingObserved: true,
      permissionDenied: false,
      queueReadonlyVisible: true,
    })
    expect(evaluateSerialized(openC4JobLogInPage, 'job-gfs')).toEqual({ clicked: true })
  })

  it('does not divert a source-marker twin of the production log helper', async () => {
    installC4OpsDom(defaultC4OpsObservation(), { jobId: 'job-gfs' })
    const twin = function openC4JobLogInPage(jobId: string) {
      void jobId
      void document.querySelector('button')
      return { clicked: 'twin' }
    }
    Object.defineProperty(twin, 'name', { value: 'openC4JobLogInPage' })
    const page = makeC4FakePage(makeC4FakePageState())
    const result = await page.evaluate(twin as never, 'job-gfs')
    expect(result).toEqual({ clicked: 'twin' })
    expect(openC4JobLogInPage('job-gfs')).toEqual({ clicked: true })
  })
})
