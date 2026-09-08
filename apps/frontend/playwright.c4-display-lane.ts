/**
 * Dedicated C4 live browser lane (#1895): current-identity preflight, `/`
 * display_readonly map proof, then strict `/ops` proof for GFS and IFS.
 * Playwright test stays thin; this module is the executable state machine.
 */

import {
  C4_HOME_READ_API_PATHS,
  C4_PER_STEP_DEADLINE_MS,
  C4_QUIET_PERIOD_MS,
  C4_WHOLE_RUN_DEADLINE_MS,
} from './src/lib/c4DisplayEvidence/constants'
import type { C4DisplayConfig } from './src/lib/c4DisplayEvidence/config'
import { buildC4OpsHref } from './src/lib/c4DisplayEvidence/config'
import {
  inspectC4HomeInPage,
  inspectC4OpsInPage,
  openC4JobLogInPage,
  type C4HomeDomObservation,
  type C4OpsDomObservation,
} from './src/lib/c4DisplayEvidence/dom'
import {
  classifyC4ControlRequest,
  extractC4JobIdFromJobsPayload,
  isC4ApplicationApiUrl,
  isC4HomeReadApiUrl,
  isC4RuntimeConfigUrl,
  matchC4OpsRequest,
} from './src/lib/c4DisplayEvidence/requestMatching'
import type {
  C4Failure,
  C4HomeCheck,
  C4NoControl,
  C4OpsCheck,
  C4ProductIdentity,
  C4RequestedPins,
} from './src/lib/c4DisplayEvidence/receipt'
import { isDisplayReadonlyRuntimeConfig } from './playwright.config.helpers'
import { createRiverClickDeadline, withRiverClickDeadline, type RiverClickDeadline } from './src/lib/riverClickEvidence/deadline'
import { c4FailureOf, resolveC4Identity, type C4LaneIdentity } from './playwright.c4-display-lane-preflight'

export interface C4LaneBrowserRequestFailure {
  errorText: string
}

export interface C4LaneBrowserRequest {
  method(): string
  url(): string
  failure?(): C4LaneBrowserRequestFailure | null
}

export interface C4LaneBrowserResponse {
  url(): string
  status(): number
  request?: () => C4LaneBrowserRequest
  finished(): Promise<unknown>
  headerValue?(name: string): Promise<string | null>
  text?(): Promise<string>
}

export interface C4LanePageSurface {
  goto(url: string): Promise<unknown>
  waitForTimeout(ms: number): Promise<unknown>
  evaluate<T>(fn: string | ((...args: unknown[]) => T | Promise<T>), ...args: unknown[]): Promise<T>
  on(
    event: 'request' | 'response' | 'requestfailed' | 'pageerror' | 'console',
    listener: (...args: never[]) => void,
  ): unknown
  off(
    event: 'request' | 'response' | 'requestfailed' | 'pageerror' | 'console',
    listener: (...args: never[]) => void,
  ): unknown
}

export interface C4LaneEnv {
  config: C4DisplayConfig
  page: C4LanePageSurface
}

export interface C4LaneTerminal {
  requestedPins: C4RequestedPins | null
  gfs: C4ProductIdentity | null
  ifs: C4ProductIdentity | null
  home: C4HomeCheck | null
  opsGfs: C4OpsCheck | null
  opsIfs: C4OpsCheck | null
  noControl: C4NoControl | null
  failure: C4Failure | null
}

export type C4LaneResult =
  | { ok: true; terminal: C4LaneTerminal }
  | { ok: false; terminal: C4LaneTerminal }

const TIMEOUT_SENTINEL = Symbol('c4-deadline-expired')
const JOBS_BODY_MAX_BYTES = 262_144

type C4ProofPhase = 'home' | 'ops'

interface ObservedRequest {
  method: string
  url: string
  phase: C4ProofPhase | null
}

interface ObservedFailedRequest {
  method: string
  url: string
  phase: C4ProofPhase | null
}

interface ObservedResponse {
  method: string
  url: string
  status: number
  completed: boolean
  completionError: boolean
  bodyState: 'not-required' | 'pending' | 'complete' | 'invalid'
}

interface ObserverState {
  requests: ObservedRequest[]
  responses: ObservedResponse[]
  failedRequests: ObservedFailedRequest[]
  forbidden: string[]
  slurmRequests: string[]
  nonGetControls: string[]
  pageErrors: number
  jsonBodies: Array<{ response: ObservedResponse; payload: unknown }>
  activeDeadline: RiverClickDeadline
  activePhase: C4ProofPhase | null
}

function timeoutValue<T>(): T {
  return TIMEOUT_SENTINEL as unknown as T
}

function failTerminal(partial: Partial<C4LaneTerminal>, failure: C4Failure): C4LaneResult {
  return {
    ok: false,
    terminal: {
      requestedPins: partial.requestedPins ?? null,
      gfs: partial.gfs ?? null,
      ifs: partial.ifs ?? null,
      home: partial.home ?? null,
      opsGfs: partial.opsGfs ?? null,
      opsIfs: partial.opsIfs ?? null,
      noControl: partial.noControl ?? null,
      failure,
    },
  }
}

function passTerminal(terminal: Omit<C4LaneTerminal, 'failure'>): C4LaneResult {
  return { ok: true, terminal: { ...terminal, failure: null } }
}

function noControlEvidence(state: ObserverState): C4NoControl {
  return {
    slurmRequestCount: state.slurmRequests.length,
    nonGetControlCount: state.nonGetControls.length,
  }
}

function requestMethodOf(response: C4LaneBrowserResponse): string {
  try {
    return response.request?.().method() ?? 'GET'
  } catch {
    return 'GET'
  }
}

async function boundedResponseText(
  response: C4LaneBrowserResponse,
  deadline: RiverClickDeadline,
): Promise<string | null> {
  if (typeof response.text !== 'function') return null
  const contentLength = typeof response.headerValue === 'function'
    ? await withRiverClickDeadline(
      response.headerValue('content-length'),
      deadline,
      () => timeoutValue<string | null>(),
    ).catch(() => null)
    : null
  if ((contentLength as unknown) === TIMEOUT_SENTINEL) return null
  if (typeof contentLength === 'string') {
    const trimmed = contentLength.trim()
    const parsed = Number(trimmed)
    if (trimmed === '' || !Number.isInteger(parsed) || parsed < 0 || parsed > JOBS_BODY_MAX_BYTES) return null
  }
  const text = await withRiverClickDeadline(
    response.text(),
    deadline,
    () => timeoutValue<string | null>(),
  ).catch(() => null)
  if ((text as unknown) === TIMEOUT_SENTINEL || text === null) return null
  if (new TextEncoder().encode(text).byteLength > JOBS_BODY_MAX_BYTES) return null
  return text
}

function markResponseCompletion(
  response: C4LaneBrowserResponse,
  observed: ObservedResponse,
  deadline: RiverClickDeadline,
): void {
  let finished: Promise<unknown>
  try {
    finished = response.finished()
  } catch {
    observed.completionError = true
    return
  }
  void withRiverClickDeadline(
    Promise.resolve(finished),
    deadline,
    () => timeoutValue<unknown>(),
  ).then(
    (result) => {
      if (result instanceof Error || result === TIMEOUT_SENTINEL) {
        observed.completionError = true
        return
      }
      observed.completed = true
    },
    () => {
      observed.completionError = true
    },
  )
}

function responseCompleted(response: ObservedResponse): boolean {
  return response.completed && !response.completionError
}

function firstCompletedResponse(
  state: ObserverState,
  predicate: (response: ObservedResponse) => boolean,
): ObservedResponse | null {
  for (const response of state.responses) {
    if (response.completionError && predicate(response)) return null
  }
  return state.responses.find((response) => responseCompleted(response) && predicate(response)) ?? null
}

function requestFailureIsNavigationAbort(request: C4LaneBrowserRequest): boolean {
  try {
    return request.failure?.()?.errorText === 'net::ERR_ABORTED'
  } catch {
    return false
  }
}

function isRequiredHomeRequest(url: string, apiOrigin: string): boolean {
  return isC4RuntimeConfigUrl(url, apiOrigin) || isC4HomeReadApiUrl(url, apiOrigin)
}

function isRequiredOpsRequest(
  method: string,
  url: string,
  apiOrigin: string,
  product: C4ProductIdentity,
): boolean {
  return matchC4OpsRequest(method, url, apiOrigin, product).matched
}

function hasRequiredHomeRequestFailure(state: ObserverState, apiOrigin: string): boolean {
  return state.failedRequests.some((request) => (
    request.phase === 'home' && isRequiredHomeRequest(request.url, apiOrigin)
  ))
}

function hasRequiredOpsRequestFailure(
  state: ObserverState,
  apiOrigin: string,
  product: C4ProductIdentity,
): boolean {
  return state.failedRequests.some((request) => (
    request.phase === 'ops' && isRequiredOpsRequest(request.method, request.url, apiOrigin, product)
  ))
}

function attachObservers(page: C4LanePageSurface, apiOrigin: string, deadline: RiverClickDeadline): {
  state: ObserverState
  detach: () => void
} {
  const state: ObserverState = {
    requests: [],
    responses: [],
    failedRequests: [],
    forbidden: [],
    slurmRequests: [],
    nonGetControls: [],
    pageErrors: 0,
    jsonBodies: [],
    activeDeadline: deadline,
    activePhase: null,
  }
  const onRequest = ((request: C4LaneBrowserRequest) => {
    const method = request.method()
    const url = request.url()
    state.requests.push({ method, url, phase: state.activePhase })
    const forbidden = classifyC4ControlRequest(method, url, apiOrigin)
    if (forbidden) {
      try {
        const parsed = new URL(url)
        const observed = `${method} ${parsed.pathname}`
        state.forbidden.push(observed)
        if (parsed.pathname.startsWith('/api/v1/slurm/')) state.slurmRequests.push(observed)
        if (method.toUpperCase() !== 'GET' && method.toUpperCase() !== 'HEAD') state.nonGetControls.push(observed)
      } catch {
        state.forbidden.push(method)
      }
    }
  }) as (...args: never[]) => void
  const onResponse = ((response: C4LaneBrowserResponse) => {
    const url = response.url()
    const status = response.status()
    const method = requestMethodOf(response)
    const observed: ObservedResponse = {
      method,
      url,
      status,
      completed: false,
      completionError: false,
      bodyState: 'not-required',
    }
    state.responses.push(observed)
    markResponseCompletion(response, observed, state.activeDeadline)
    let pathname = ''
    try {
      pathname = new URL(url).pathname
    } catch {
      pathname = ''
    }
    const captureBody = isC4RuntimeConfigUrl(url, apiOrigin) || pathname === '/api/v1/jobs'
    if (captureBody) {
      observed.bodyState = 'pending'
      const capture = boundedResponseText(response, state.activeDeadline).then((text) => {
        if (text === null) {
          observed.bodyState = 'invalid'
          return
        }
        try {
          state.jsonBodies.push({ response: observed, payload: JSON.parse(text) as unknown })
          observed.bodyState = 'complete'
        } catch {
          observed.bodyState = 'invalid'
        }
      }).catch(() => {
        observed.bodyState = 'invalid'
      })
    }
  }) as (...args: never[]) => void
  const onFailed = ((request: C4LaneBrowserRequest) => {
    const url = request.url()
    if (!isC4ApplicationApiUrl(url, apiOrigin) || requestFailureIsNavigationAbort(request)) return
    state.failedRequests.push({ method: request.method(), url, phase: state.activePhase })
  }) as (...args: never[]) => void
  const onPageError = (() => {
    state.pageErrors += 1
  }) as (...args: never[]) => void
  const onConsole = (() => undefined) as (...args: never[]) => void

  page.on('request', onRequest)
  page.on('response', onResponse)
  page.on('requestfailed', onFailed)
  page.on('pageerror', onPageError)
  page.on('console', onConsole)

  return {
    state,
    detach: () => {
      page.off('request', onRequest)
      page.off('response', onResponse)
      page.off('requestfailed', onFailed)
      page.off('pageerror', onPageError)
      page.off('console', onConsole)
    },
  }
}

async function pollUntil<T>(
  page: C4LanePageSurface,
  deadline: RiverClickDeadline,
  probe: () => Promise<T | null>,
): Promise<T | typeof TIMEOUT_SENTINEL> {
  for (;;) {
    if (deadline.expired()) return TIMEOUT_SENTINEL
    await Promise.resolve()
    if (deadline.expired()) return TIMEOUT_SENTINEL
    const value = await withRiverClickDeadline(probe(), deadline, () => timeoutValue<T | null>())
    if ((value as unknown) === TIMEOUT_SENTINEL) return TIMEOUT_SENTINEL
    if (value !== null) return value as T
    const remaining = Math.min(50, deadline.remaining())
    if (remaining <= 0) return TIMEOUT_SENTINEL
    const waited = await withRiverClickDeadline(page.waitForTimeout(remaining), deadline, () => timeoutValue<unknown>())
    if ((waited as unknown) === TIMEOUT_SENTINEL) return TIMEOUT_SENTINEL
  }
}

async function evaluateWithinC4Step<T>(
  page: C4LanePageSurface,
  expression: () => T,
  step: RiverClickDeadline,
): Promise<T | typeof TIMEOUT_SENTINEL> {
  if (step.expired()) return TIMEOUT_SENTINEL
  let evaluation: Promise<T>
  try {
    evaluation = page.evaluate(expression as never) as Promise<T>
  } catch {
    return TIMEOUT_SENTINEL
  }
  const observed = await withRiverClickDeadline(
    evaluation,
    step,
    () => timeoutValue<T>(),
  ).catch(() => timeoutValue<T>())
  if ((observed as unknown) === TIMEOUT_SENTINEL || step.expired()) return TIMEOUT_SENTINEL
  return observed
}

function homeReady(
  observation: C4HomeDomObservation,
  state: ObserverState,
  apiOrigin: string,
): C4HomeCheck | C4Failure | null {
  if (state.forbidden.length > 0) {
    return c4FailureOf('FORBIDDEN_CONTROL', 'home', 'home issued a forbidden control request')
  }
  if (hasRequiredHomeRequestFailure(state, apiOrigin)) {
    return c4FailureOf('HOME_NOT_READY', 'home', 'required home API request failed during home proof')
  }
  if (state.pageErrors > 0) {
    return c4FailureOf('HOME_NOT_READY', 'home', 'page error during home proof')
  }
  const runtimeCandidates = state.responses.filter((response) => isC4RuntimeConfigUrl(response.url, apiOrigin))
  if (runtimeCandidates.some((response) => response.completionError)) {
    return c4FailureOf('RUNTIME_CONFIG_INVALID', 'home', 'runtime config response failed to complete')
  }
  const runtimeParsed = runtimeCandidates.map((response) => ({
    response,
    body: state.jsonBodies.find((entry) => entry.response === response)?.payload,
  }))
  const runtimeInvalid = runtimeParsed.find((entry) => responseCompleted(entry.response) && entry.body !== undefined && (
    entry.response.status < 200 ||
    entry.response.status > 299 ||
    !isDisplayReadonlyRuntimeConfig(entry.body)
  ))
  if (runtimeInvalid) {
    return c4FailureOf('RUNTIME_CONFIG_INVALID', 'home', 'runtime config is not display_readonly')
  }
  const runtime = runtimeParsed.find((entry) => (
    responseCompleted(entry.response) &&
    entry.response.bodyState === 'complete' &&
    entry.response.status >= 200 &&
    entry.response.status <= 299 &&
    entry.body !== undefined &&
    isDisplayReadonlyRuntimeConfig(entry.body)
  ))?.response
  const homeReadFailedCompletion = state.responses.some((response) => (
    response.completionError && isC4HomeReadApiUrl(response.url, apiOrigin)
  ))
  if (homeReadFailedCompletion) return c4FailureOf('HOME_NOT_READY', 'home', 'home read response failed to complete')
  const currentRead = firstCompletedResponse(state, (response) => (
    isC4HomeReadApiUrl(response.url, apiOrigin) && response.status >= 200 && response.status <= 299
  ))
  if (observation.mapSourceError) {
    return c4FailureOf('HOME_NOT_READY', 'home', 'home map source error contradicts readiness')
  }
  if (!observation.mapPresent || observation.mapWidth <= 0 || observation.mapHeight <= 0) {
    if (observation.empty || observation.mapUnavailable) {
      return c4FailureOf('HOME_NOT_READY', 'home', 'home map terminal contradicts readiness')
    }
    return null
  }
  if (observation.empty || observation.mapUnavailable) {
    return c4FailureOf('HOME_NOT_READY', 'home', 'home map terminal contradicts readiness')
  }
  if (observation.loading) return null
  if (!runtime) return null
  if (!currentRead) return null
  let currentReadPath = '/api/v1/basins'
  try {
    currentReadPath = new URL(currentRead.url).pathname
    if (!(C4_HOME_READ_API_PATHS as readonly string[]).includes(currentReadPath)) {
      currentReadPath = '/api/v1/basins'
    }
  } catch {
    currentReadPath = '/api/v1/basins'
  }
  return {
    path: '/',
    mapSurfaceVisible: true,
    runtimeConfigStatus: runtime.status,
    runtimeServiceRole: 'display_readonly',
    currentReadObserved: true,
    currentReadPath,
  }
}

function opsEvidence(
  observation: C4OpsDomObservation,
  state: ObserverState,
  apiOrigin: string,
  product: C4ProductIdentity,
  startedAtLength: { requests: number; responses: number; forbidden: number; pageErrors: number },
): C4OpsCheck | C4Failure | null {
  const newForbidden = state.forbidden.slice(startedAtLength.forbidden)
  if (newForbidden.length > 0) return c4FailureOf('FORBIDDEN_CONTROL', 'ops', 'ops issued a forbidden control request')
  if (hasRequiredOpsRequestFailure(state, apiOrigin, product)) {
    return c4FailureOf('OPS_UNAVAILABLE', 'ops', 'required ops API request failed during ops proof')
  }
  if (state.pageErrors > startedAtLength.pageErrors) {
    return c4FailureOf('OPS_UNAVAILABLE', 'ops', 'page error during ops proof')
  }
  if (observation.permissionDenied) {
    return c4FailureOf('PERMISSION_DENIED', 'ops', 'production bundle role cannot access /ops')
  }
  if (observation.runtimeUnavailable) {
    return c4FailureOf('OPS_UNAVAILABLE', 'ops', 'runtime config is unavailable on /ops')
  }
  if (!observation.headingObserved) return null
  if (observation.roleSelectorCount !== 0) {
    return c4FailureOf('FORBIDDEN_CONTROL', 'ops', 'role selector is visible on display_readonly /ops')
  }
  if (observation.retryCancelControlCount !== 0) {
    return c4FailureOf('FORBIDDEN_CONTROL', 'ops', 'retry or cancel control is visible on display_readonly /ops')
  }
  const kindOf = (response: ObservedResponse, kind: 'status' | 'stages' | 'jobs' | 'logs') => {
    const match = matchC4OpsRequest(response.method, response.url, apiOrigin, product)
    return match.matched && match.kind === kind
  }
  const failedCompletion = state.responses.find((response) => (
    response.completionError && (
      kindOf(response, 'status') ||
      kindOf(response, 'stages') ||
      kindOf(response, 'jobs') ||
      kindOf(response, 'logs')
    )
  ))
  if (failedCompletion) return c4FailureOf('OPS_UNAVAILABLE', 'ops', 'strict ops response failed to complete')
  const status = firstCompletedResponse(state, (response) => kindOf(response, 'status') && response.status >= 200 && response.status <= 299)
  const stages = firstCompletedResponse(state, (response) => kindOf(response, 'stages') && response.status >= 200 && response.status <= 299)
  const jobs = firstCompletedResponse(state, (response) => kindOf(response, 'jobs') && response.status >= 200 && response.status <= 299)
  if (!status || !stages || !jobs) return null
  const jobsPayload = state.jsonBodies.find((entry) => entry.response === jobs)?.payload
  if (jobs.bodyState === 'pending') return null
  const jobId = jobsPayload ? extractC4JobIdFromJobsPayload(jobsPayload, product) : null
  if (!jobId && jobs.bodyState === 'invalid') return null
  if (!jobId) return c4FailureOf('JOB_LOG_MISSING', 'ops', 'jobs response has no current-identity job')
  const logs = firstCompletedResponse(state, (response) => {
    const match = matchC4OpsRequest(response.method, response.url, apiOrigin, product)
    return match.matched && match.kind === 'logs' && match.jobId === jobId && response.status >= 200 && response.status <= 299
  })
  if (!logs) return null
  if (!observation.queueReadonlyVisible || !observation.operatorRecoveryVisible) return null
  return {
    sourceId: product.sourceId,
    path: '/ops',
    headingObserved: true,
    permissionDenied: false,
    runtimeUnavailable: false,
    statusStatus: status.status,
    stagesStatus: stages.status,
    jobsStatus: jobs.status,
    jobId,
    logsStatus: logs.status,
    roleSelectorCount: 0,
    retryCancelControlCount: 0,
    slurmRequestCount: state.slurmRequests.length,
    nonGetControlCount: state.nonGetControls.length,
    queueReadonlyVisible: true,
    operatorRecoveryVisible: true,
  }
}

async function proveHome(
  env: C4LaneEnv,
  state: ObserverState,
  deadline: RiverClickDeadline,
): Promise<C4HomeCheck | C4Failure> {
  const step = createRiverClickDeadline(
    Math.min(C4_PER_STEP_DEADLINE_MS, deadline.remaining()),
    () => deadline.now(),
    deadline.now(),
  )
  state.activeDeadline = step
  state.activePhase = 'home'
  const navigated = await withRiverClickDeadline(env.page.goto('/'), step, () => timeoutValue<unknown>())
  if ((navigated as unknown) === TIMEOUT_SENTINEL || deadline.expired()) {
    return c4FailureOf('STEP_TIMEOUT', 'home', 'home navigation exceeded the per-step deadline')
  }
  if (hasRequiredHomeRequestFailure(state, env.config.apiOrigin)) {
    return c4FailureOf('HOME_NOT_READY', 'home', 'required home API request failed during home proof')
  }
  for (;;) {
    const ready = await pollUntil(env.page, step, async () => {
      const observation = await evaluateWithinC4Step<C4HomeDomObservation>(
        env.page,
        inspectC4HomeInPage,
        step,
      )
      if (observation === TIMEOUT_SENTINEL) return c4FailureOf('STEP_TIMEOUT', 'home', 'home observation exceeded the per-step deadline')
      const result = homeReady(observation, state, env.config.apiOrigin)
      if (result === null) return null
      return result
    })
    if ((ready as unknown) === TIMEOUT_SENTINEL) {
      return c4FailureOf('STEP_TIMEOUT', 'home', 'home readiness exceeded the per-step deadline')
    }
    if ('code' in (ready as C4HomeCheck | C4Failure)) return ready as C4Failure
    const quiet = await withRiverClickDeadline(env.page.waitForTimeout(C4_QUIET_PERIOD_MS), step, () => timeoutValue<unknown>())
    if ((quiet as unknown) === TIMEOUT_SENTINEL || step.expired()) {
      return c4FailureOf('STEP_TIMEOUT', 'home', 'home quiet period exceeded the per-step deadline')
    }
    const after = await evaluateWithinC4Step<C4HomeDomObservation>(
      env.page,
      inspectC4HomeInPage,
      step,
    )
    if (after === TIMEOUT_SENTINEL || step.expired()) {
      return c4FailureOf('STEP_TIMEOUT', 'home', 'home quiet confirmation exceeded the per-step deadline')
    }
    const confirmed = homeReady(after, state, env.config.apiOrigin)
    if (confirmed === null && after.loading) continue
    if (confirmed === null) {
      return c4FailureOf('HOME_NOT_READY', 'home', 'home readiness drifted during quiet period')
    }
    if ('code' in confirmed) return confirmed
    return confirmed
  }
}

async function proveOps(
  env: C4LaneEnv,
  state: ObserverState,
  product: C4ProductIdentity,
  deadline: RiverClickDeadline,
): Promise<C4OpsCheck | C4Failure> {
  const step = createRiverClickDeadline(
    Math.min(C4_PER_STEP_DEADLINE_MS, deadline.remaining()),
    () => deadline.now(),
    deadline.now(),
  )
  state.activeDeadline = step
  state.activePhase = 'ops'
  const href = buildC4OpsHref(product)
  const marker = {
    requests: state.requests.length,
    responses: state.responses.length,
    forbidden: state.forbidden.length,
    pageErrors: state.pageErrors,
  }
  const navigated = await withRiverClickDeadline(env.page.goto(href), step, () => timeoutValue<unknown>())
  if ((navigated as unknown) === TIMEOUT_SENTINEL || deadline.expired()) {
    return c4FailureOf('STEP_TIMEOUT', 'ops', 'ops navigation exceeded the per-step deadline')
  }
  if (hasRequiredOpsRequestFailure(state, env.config.apiOrigin, product)) {
    return c4FailureOf('OPS_UNAVAILABLE', 'ops', 'required ops API request failed during ops proof')
  }
  const ready = await pollUntil(env.page, step, async () => {
    const observation = await evaluateWithinC4Step<C4OpsDomObservation>(
      env.page,
      inspectC4OpsInPage,
      step,
    )
    if (observation === TIMEOUT_SENTINEL) return c4FailureOf('STEP_TIMEOUT', 'ops', 'ops observation exceeded the per-step deadline')
    const result = opsEvidence(observation, state, env.config.apiOrigin, product, marker)
    if (result === null) {
      const jobs = firstCompletedResponse(state, (response) => {
        const match = matchC4OpsRequest(response.method, response.url, env.config.apiOrigin, product)
        return match.matched && match.kind === 'jobs' && response.status >= 200 && response.status <= 299
      })
      if (jobs) {
        const payload = state.jsonBodies.find((entry) => entry.response === jobs)?.payload
        const jobId = payload ? extractC4JobIdFromJobsPayload(payload, product) : null
        if (jobId) {
          const logs = state.responses.some((response) => {
            const match = matchC4OpsRequest(response.method, response.url, env.config.apiOrigin, product)
            return responseCompleted(response) && match.matched && match.kind === 'logs' && match.jobId === jobId
          })
          if (!logs) {
            await env.page.evaluate(openC4JobLogInPage as never, jobId)
          }
        }
      }
      return null
    }
    return result
  })
  if ((ready as unknown) === TIMEOUT_SENTINEL) {
    return c4FailureOf('STEP_TIMEOUT', 'ops', 'ops readiness exceeded the per-step deadline')
  }
  if ('code' in (ready as C4OpsCheck | C4Failure)) return ready as C4Failure
  const quiet = await withRiverClickDeadline(env.page.waitForTimeout(C4_QUIET_PERIOD_MS), step, () => timeoutValue<unknown>())
  if ((quiet as unknown) === TIMEOUT_SENTINEL || step.expired()) {
    return c4FailureOf('STEP_TIMEOUT', 'ops', 'ops quiet period exceeded the per-step deadline')
  }
  const after = await evaluateWithinC4Step<C4OpsDomObservation>(
    env.page,
    inspectC4OpsInPage,
    step,
  )
  if (after === TIMEOUT_SENTINEL || step.expired()) {
    return c4FailureOf('STEP_TIMEOUT', 'ops', 'ops quiet confirmation exceeded the per-step deadline')
  }
  const confirmed = opsEvidence(after, state, env.config.apiOrigin, product, marker)
  if (confirmed === null || 'code' in confirmed) {
    return (confirmed && 'code' in confirmed)
      ? confirmed
      : c4FailureOf('OPS_UNAVAILABLE', 'ops', 'ops readiness drifted during quiet period')
  }
  return confirmed
}

export async function runC4DisplayLane(
  env: C4LaneEnv,
  fetchImpl: (url: string, init: RequestInit) => Promise<Response>,
  options: { deadline?: RiverClickDeadline } = {},
): Promise<C4LaneResult> {
  const deadline = options.deadline ?? createRiverClickDeadline(C4_WHOLE_RUN_DEADLINE_MS)
  const observers = attachObservers(env.page, env.config.apiOrigin, deadline)
  try {
    if (deadline.expired()) {
      return failTerminal({ noControl: noControlEvidence(observers.state) }, c4FailureOf('WHOLE_RUN_TIMEOUT', 'timeout', 'whole-run deadline exceeded before preflight'))
    }
    const identity = await resolveC4Identity(env.config, fetchImpl, deadline)
    if (!identity.ok) return failTerminal({ noControl: noControlEvidence(observers.state) }, identity.failure)
    const pins = identity.identity.requestedPins
    const gfs = identity.identity.gfs
    const ifs = identity.identity.ifs
    if (deadline.expired()) {
      return failTerminal({ requestedPins: pins, gfs, ifs, noControl: noControlEvidence(observers.state) }, c4FailureOf('WHOLE_RUN_TIMEOUT', 'timeout', 'whole-run deadline exceeded after preflight'))
    }
    const home = await proveHome(env, observers.state, deadline)
    if ('code' in home) return failTerminal({ requestedPins: pins, gfs, ifs, noControl: noControlEvidence(observers.state) }, home)
    if (deadline.expired()) {
      return failTerminal({ requestedPins: pins, gfs, ifs, home, noControl: noControlEvidence(observers.state) }, c4FailureOf('WHOLE_RUN_TIMEOUT', 'timeout', 'whole-run deadline exceeded after home'))
    }
    const opsGfs = await proveOps(env, observers.state, gfs, deadline)
    if ('code' in opsGfs) return failTerminal({ requestedPins: pins, gfs, ifs, home, noControl: noControlEvidence(observers.state) }, opsGfs)
    if (deadline.expired()) {
      return failTerminal({ requestedPins: pins, gfs, ifs, home, opsGfs, noControl: noControlEvidence(observers.state) }, c4FailureOf('WHOLE_RUN_TIMEOUT', 'timeout', 'whole-run deadline exceeded after GFS ops'))
    }
    const opsIfs = await proveOps(env, observers.state, ifs, deadline)
    if ('code' in opsIfs) return failTerminal({ requestedPins: pins, gfs, ifs, home, opsGfs, noControl: noControlEvidence(observers.state) }, opsIfs)
    if (gfs.runId === ifs.runId && gfs.modelId === ifs.modelId && gfs.cycleTime === ifs.cycleTime) {
      return failTerminal(
        { requestedPins: pins, gfs, ifs, home, opsGfs, opsIfs, noControl: noControlEvidence(observers.state) },
        c4FailureOf('SOURCE_SWITCH_FAILED', 'source_switch', 'GFS and IFS identities are not distinct or source-bound'),
      )
    }
    const remaining = observers.state.forbidden
    if (remaining.length > 0) {
      return failTerminal(
        { requestedPins: pins, gfs, ifs, home, opsGfs, opsIfs, noControl: noControlEvidence(observers.state) },
        c4FailureOf('FORBIDDEN_CONTROL', 'ops', 'forbidden control request observed across the C4 run'),
      )
    }
    if (observers.state.slurmRequests.length !== 0 || observers.state.nonGetControls.length !== 0) {
      return failTerminal(
        { requestedPins: pins, gfs, ifs, home, opsGfs, opsIfs, noControl: noControlEvidence(observers.state) },
        c4FailureOf('FORBIDDEN_CONTROL', 'ops', 'observed control request count is non-zero'),
      )
    }
    return passTerminal({
      requestedPins: pins,
      gfs,
      ifs,
      home,
      opsGfs,
      opsIfs,
      noControl: noControlEvidence(observers.state),
    })
  } catch {
    return failTerminal(
      { noControl: noControlEvidence(observers.state) },
      c4FailureOf('INTERNAL_ERROR', 'runtime', 'C4 lane internal error'),
    )
  } finally {
    observers.detach()
  }
}

export type { C4LaneIdentity, C4HomeDomObservation, C4OpsDomObservation }
