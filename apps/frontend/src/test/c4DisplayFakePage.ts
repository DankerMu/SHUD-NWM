/**
 * Faithful fake Playwright page for the C4 live lane unit tests.
 *
 * Evaluate routing is identity-based: only the imported production helpers
 * `inspectC4HomeInPage`, `inspectC4OpsInPage`, and `openC4JobLogInPage` may be
 * diverted to the injected state-machine. Source text and function names are
 * never consulted.
 */

import { vi } from 'vitest'

import { inspectC4HomeInPage, inspectC4OpsInPage, openC4JobLogInPage } from '../lib/c4DisplayEvidence/dom'
import type { C4HomeDomObservation, C4OpsDomObservation } from '../lib/c4DisplayEvidence/dom'
import type { C4LaneBrowserRequest, C4LaneBrowserResponse, C4LanePageSurface } from '../../playwright.c4-display-lane'

export interface C4FakeRequestFailure {
  errorText: string
}

export interface C4FakeResponseSpec {
  method?: string
  url: string
  status: number
  body?: unknown
  contentLength?: string | null
  headerValue?: (name: string) => Promise<string | null>
  finished?: () => Promise<unknown>
  text?: () => Promise<string>
  textCallCount?: { value: number }
}

export interface C4FakePageState {
  listeners: Record<string, Array<(arg: unknown) => void>>
  gotoUrls: string[]
  homeObservation: C4HomeDomObservation
  homeObservations?: C4HomeDomObservation[]
  opsObservation: C4OpsDomObservation
  homeResponses: C4FakeResponseSpec[]
  opsResponsesFor: (url: string) => C4FakeResponseSpec[]
  logResponsesFor: (jobId: string, opsUrl: string) => C4FakeResponseSpec[]
  failRequests?: Array<C4FakeResponseSpec & { failure?: C4FakeRequestFailure }>
  sleepMs?: number
  extraRequestsOnGoto?: (url: string) => Array<{ method: string; url: string }>
}

export function defaultC4HomeObservation(): C4HomeDomObservation {
  return {
    mapPresent: true,
    mapWidth: 800,
    mapHeight: 600,
    loading: false,
    empty: false,
    mapUnavailable: false,
    mapSourceError: false,
  }
}

export function defaultC4OpsObservation(): C4OpsDomObservation {
  return {
    headingObserved: true,
    permissionDenied: false,
    runtimeUnavailable: false,
    roleSelectorCount: 0,
    retryCancelControlCount: 0,
    queueReadonlyVisible: true,
    operatorRecoveryVisible: true,
  }
}

export function makeC4FakePageState(overrides: Partial<C4FakePageState> = {}): C4FakePageState {
  return {
    listeners: { request: [], response: [], requestfailed: [], pageerror: [], console: [] },
    gotoUrls: [],
    homeObservation: defaultC4HomeObservation(),
    opsObservation: defaultC4OpsObservation(),
    homeResponses: [],
    opsResponsesFor: () => [],
    logResponsesFor: () => [],
    sleepMs: 1,
    ...overrides,
  }
}

function fakeRequest(method: string, url: string, failure: C4FakeRequestFailure | null = null): C4LaneBrowserRequest {
  return {
    method: () => method,
    url: () => url,
    failure: () => failure,
  }
}

function fakeResponse(spec: C4FakeResponseSpec): C4LaneBrowserResponse {
  const method = spec.method ?? 'GET'
  const body = spec.body === undefined ? '' : JSON.stringify(spec.body)
  return {
    url: () => spec.url,
    status: () => spec.status,
    request: () => fakeRequest(method, spec.url),
    finished: spec.finished ?? (() => Promise.resolve(null)),
    headerValue: spec.headerValue ?? (async (name: string) => {
      if (name === 'content-length') return spec.contentLength === undefined
        ? String(new TextEncoder().encode(body).byteLength)
        : spec.contentLength
      if (name === 'content-encoding') return 'identity'
      return null
    }),
    text: async () => {
      if (spec.textCallCount) spec.textCallCount.value += 1
      return spec.text ? spec.text() : body
    },
  }
}

function emit(state: C4FakePageState, event: string, arg: unknown) {
  for (const listener of state.listeners[event] ?? []) listener(arg)
}

function emitResponse(state: C4FakePageState, spec: C4FakeResponseSpec) {
  const method = spec.method ?? 'GET'
  emit(state, 'request', fakeRequest(method, spec.url))
  emit(state, 'response', fakeResponse(spec))
}

export function installC4HomeDom(observation: C4HomeDomObservation = defaultC4HomeObservation()): void {
  document.body.innerHTML = ''
  const map = document.createElement('div')
  map.setAttribute('data-testid', 'm11-map-surface')
  map.getBoundingClientRect = () => ({
    x: 0,
    y: 0,
    top: 0,
    left: 0,
    right: observation.mapWidth,
    bottom: observation.mapHeight,
    width: observation.mapWidth,
    height: observation.mapHeight,
    toJSON() {
      return this
    },
  }) as DOMRect
  document.body.appendChild(map)
  if (observation.loading) {
    const loading = document.createElement('div')
    loading.setAttribute('data-testid', 'm11-overview-loading')
    loading.textContent = '总览数据加载中'
    document.body.appendChild(loading)
  }
  if (observation.empty) {
    const empty = document.createElement('div')
    empty.setAttribute('data-testid', 'm11-overview-empty')
    empty.textContent = '暂无可用流域数据'
    document.body.appendChild(empty)
  }
  if (observation.mapUnavailable) {
    const unavailable = document.createElement('div')
    unavailable.setAttribute('data-testid', 'm11-map-unavailable')
    unavailable.textContent = '地图不可用'
    document.body.appendChild(unavailable)
  }
  if (observation.mapSourceError) {
    const sourceError = document.createElement('div')
    sourceError.setAttribute('data-testid', 'm11-map-source-error')
    sourceError.textContent = '地图数据源加载失败'
    document.body.appendChild(sourceError)
  }
}

export function installC4OpsDom(
  observation: C4OpsDomObservation = defaultC4OpsObservation(),
  options: { jobId?: string } = {},
): void {
  document.body.innerHTML = ''
  if (observation.headingObserved) {
    const heading = document.createElement('h1')
    heading.textContent = '内部诊断'
    document.body.appendChild(heading)
  }
  if (observation.permissionDenied) {
    const alert = document.createElement('div')
    alert.setAttribute('role', 'alert')
    const title = document.createElement('h2')
    title.textContent = '权限不足'
    alert.appendChild(title)
    document.body.appendChild(alert)
  }
  if (observation.runtimeUnavailable) {
    const status = document.createElement('div')
    status.setAttribute('role', 'status')
    status.textContent = 'runtime config 不可用：加载失败'
    document.body.appendChild(status)
  }
  if (observation.roleSelectorCount > 0) {
    const role = document.createElement('select')
    role.setAttribute('aria-label', 'Role')
    document.body.appendChild(role)
  }
  if (observation.retryCancelControlCount > 0) {
    const retry = document.createElement('button')
    retry.textContent = '重试'
    document.body.appendChild(retry)
  }
  if (observation.queueReadonlyVisible) {
    const queue = document.createElement('div')
    queue.setAttribute('role', 'status')
    queue.textContent = 'display_readonly 只读展示节点不读取 Slurm 队列深度；阶段、作业和日志仍可查看。'
    document.body.appendChild(queue)
  }
  if (observation.operatorRecoveryVisible) {
    const guidance = document.createElement('div')
    guidance.setAttribute('data-testid', 'ops-manual-recovery-guidance')
    guidance.textContent = '失败诊断用于交给 22 compute-control 节点处理；需要恢复时在 22 compute-control 节点按恢复 runbook 处理。'
    document.body.appendChild(guidance)
  }
  if (options.jobId) {
    const table = document.createElement('table')
    const row = document.createElement('tr')
    const cell = document.createElement('td')
    cell.textContent = options.jobId
    const action = document.createElement('td')
    const logButton = document.createElement('button')
    logButton.textContent = '查看日志'
    action.appendChild(logButton)
    row.appendChild(cell)
    row.appendChild(action)
    table.appendChild(row)
    document.body.appendChild(table)
  }
}

export function makeC4FakePage(state: C4FakePageState): C4LanePageSurface {
  let homeObservationIndex = 0
  const currentHomeObservation = () => {
    const observations = state.homeObservations
    if (!observations || observations.length === 0) return state.homeObservation
    const observation = observations[Math.min(homeObservationIndex, observations.length - 1)]
    homeObservationIndex += 1
    return observation
  }
  return {
    goto: vi.fn(async (url: string) => {
      state.gotoUrls.push(url)
      const extras = state.extraRequestsOnGoto?.(url) ?? []
      for (const extra of extras) emit(state, 'request', fakeRequest(extra.method, extra.url))
      if (url === '/' || url.endsWith('/')) {
        installC4HomeDom(state.homeObservation)
        for (const spec of state.homeResponses) emitResponse(state, spec)
      } else if (url.includes('/ops')) {
        const jobId = state.opsResponsesFor(url).flatMap((spec) => {
          const items = (spec.body as { data?: { items?: Array<{ job_id?: string }> } } | undefined)?.data?.items
          return items?.map((item) => item.job_id).filter((value): value is string => Boolean(value)) ?? []
        })[0]
        installC4OpsDom(state.opsObservation, { jobId })
        for (const spec of state.opsResponsesFor(url)) emitResponse(state, spec)
      }
      for (const spec of state.failRequests ?? []) {
        const request = fakeRequest(spec.method ?? 'GET', spec.url, spec.failure ?? null)
        emit(state, 'request', request)
        emit(state, 'requestfailed', request)
      }
    }),
    waitForTimeout: vi.fn(async (ms: number) => {
      const sleepMs = state.sleepMs
      if (sleepMs) await new Promise((resolve) => setTimeout(resolve, Math.min(sleepMs, ms)))
    }),
    evaluate: vi.fn(async (expr: unknown, ...args: unknown[]) => {
      if (typeof expr === 'function') {
        const fn = expr as (arg?: unknown) => unknown
        if (fn === inspectC4HomeInPage) {
          installC4HomeDom(currentHomeObservation())
          return inspectC4HomeInPage()
        }
        if (fn === inspectC4OpsInPage) {
          return inspectC4OpsInPage()
        }
        if (fn === openC4JobLogInPage) {
          const jobId = String(args[0] ?? '')
          const result = openC4JobLogInPage(jobId)
          if (result.clicked) {
            const lastOps = [...state.gotoUrls].reverse().find((value) => value.includes('/ops')) ?? ''
            for (const spec of state.logResponsesFor(jobId, lastOps)) emitResponse(state, spec)
          }
          return result
        }
        return args.length > 0 ? fn(args[0]) : fn()
      }
      return undefined
    }) as never,
    on: vi.fn((event: string, listener: (arg: unknown) => void) => {
      if (!state.listeners[event]) state.listeners[event] = []
      state.listeners[event].push(listener)
    }) as never,
    off: vi.fn((event: string, listener: (arg: unknown) => void) => {
      const list = state.listeners[event] ?? []
      const index = list.indexOf(listener)
      if (index >= 0) list.splice(index, 1)
    }) as never,
  }
}
