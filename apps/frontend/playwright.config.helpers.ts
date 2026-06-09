import { existsSync, lstatSync, readdirSync, readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import path from 'node:path'

export interface LiveDisplayEnv {
  baseURL: string
  apiBaseURL: string
  viteApiBaseURL: string
}

export interface LiveDisplayRuntimeConfig {
  display_readonly?: boolean
  service_role?: string
}

export type LiveDisplayApiBindingMode = 'distinct-api' | 'same-origin-proxy'

export interface LiveDisplayApiBinding {
  mode: LiveDisplayApiBindingMode
  expectedOrigin: string
}

export interface LiveDisplayBrowserResponse {
  url: string
  status: number
  body?: unknown
  parseError?: string
}

export interface LiveDisplayPageEvidence {
  runtimeConfigResponses: LiveDisplayBrowserResponse[]
  readApiResponses: LiveDisplayBrowserResponse[]
  forbiddenControlRequests: string[]
  permissionDeniedVisible: boolean
  runtimeConfigUnavailableVisible: boolean
}

const liveDisplayRequiredEnv = ['PLAYWRIGHT_LIVE_BASE_URL', 'PLAYWRIGHT_LIVE_API_BASE_URL'] as const
export const liveDisplaySpecPattern = /(^|[/\\])live-display\.spec\.ts$/
const broadApiRouteMockPattern = /page\s*\.\s*route\s*\(\s*(['"`])\*\*\/api\/v1\/\*\*\1/g
const liveDisplayRuntimeConfigMaxBytes = 4096
const generatedDirNames = new Set(['node_modules', 'dist', 'coverage', 'playwright-report', 'test-results'])
const monitoringReadApiPaths = new Set([
  '/api/v1/pipeline/status',
  '/api/v1/pipeline/stages',
  '/api/v1/jobs',
])

export interface LiveDisplayResponseSource {
  url(): string
  status(): number
  headerValue(name: string): Promise<string | null>
  text(): Promise<string>
}

export function parsePlaywrightWorkers(value: string | undefined) {
  if (value === undefined || value.trim() === '') return 1
  const parsed = Number(value)
  if (!Number.isInteger(parsed) || parsed < 1) {
    throw new Error('PLAYWRIGHT_WORKERS must be a positive integer.')
  }
  return Math.min(parsed, 4)
}

function normalizeHttpUrl(name: string, value: string) {
  const trimmed = value.trim()
  let parsed: URL
  try {
    parsed = new URL(trimmed)
  } catch {
    throw new Error(`${name} must be an absolute http(s) URL for the live display Playwright profile.`)
  }
  if (parsed.protocol !== 'http:' && parsed.protocol !== 'https:') {
    throw new Error(`${name} must use http or https for the live display Playwright profile.`)
  }
  if (parsed.username || parsed.password) {
    throw new Error(
      `${name} must not include username/password userinfo for the live display Playwright profile.`,
    )
  }
  return trimmed
}

export function loadLiveDisplayEnv(env: NodeJS.ProcessEnv = process.env): LiveDisplayEnv {
  const missing = liveDisplayRequiredEnv.filter((name) => !env[name]?.trim())
  if (missing.length > 0) {
    throw new Error(
      `Live display Playwright profile BLOCKED: missing ${missing.join(', ')}. ` +
        'Set PLAYWRIGHT_LIVE_BASE_URL to the live display frontend URL and ' +
        'PLAYWRIGHT_LIVE_API_BASE_URL to the live display API URL; this profile has no local dev server or https://api.example.test fallback.',
    )
  }

  const baseURL = normalizeHttpUrl('PLAYWRIGHT_LIVE_BASE_URL', env.PLAYWRIGHT_LIVE_BASE_URL as string)
  const apiBaseURL = normalizeHttpUrl('PLAYWRIGHT_LIVE_API_BASE_URL', env.PLAYWRIGHT_LIVE_API_BASE_URL as string)

  return { baseURL, apiBaseURL, viteApiBaseURL: apiBaseURL }
}

function pathFromInput(input: string | URL) {
  return input instanceof URL ? fileURLToPath(input) : input
}

export function findLiveDisplaySpecFiles(testDir: string | URL) {
  const root = path.resolve(pathFromInput(testDir))
  if (!existsSync(root)) return []

  const files: string[] = []
  const visit = (entry: string) => {
    const resolved = path.resolve(entry)
    const relative = path.relative(root, resolved)
    if (relative === '..' || relative.startsWith(`..${path.sep}`) || path.isAbsolute(relative)) return

    const stat = lstatSync(resolved)
    if (stat.isSymbolicLink()) return
    if (stat.isDirectory()) {
      if (generatedDirNames.has(path.basename(resolved))) return
      for (const child of readdirSync(resolved)) visit(path.join(resolved, child))
      return
    }
    if (stat.isFile() && isLiveDisplaySpecFile(resolved)) files.push(resolved)
  }

  visit(root)
  return files.sort()
}

export function isLiveDisplaySpecFile(file: string) {
  return liveDisplaySpecPattern.test(file)
}

export function findBroadApiRouteMocks(files: string[]) {
  return files.flatMap((file) => {
    const content = readFileSync(file, 'utf8')
    return [...content.matchAll(broadApiRouteMockPattern)].map((match) => ({
      file,
      line: content.slice(0, match.index).split('\n').length,
    }))
  })
}

export function assertNoBroadApiRouteMocks(files: string[], laneName = 'live display') {
  const violations = findBroadApiRouteMocks(files)
  if (violations.length === 0) return

  const locations = violations.map((violation) => `${violation.file}:${violation.line}`).join(', ')
  throw new Error(
    `${laneName} Playwright specs cannot register broad page.route('**/api/v1/**') API mocks: ${locations}`,
  )
}

export function assertLiveDisplaySpecsDoNotMockApis(testDir: string | URL) {
  const files = findLiveDisplaySpecFiles(testDir)
  assertNoBroadApiRouteMocks(files, 'live display_readonly')
  return files
}

export function unwrapApiData(value: unknown) {
  if (value && typeof value === 'object' && 'data' in value) {
    return (value as { data: unknown }).data
  }
  return value
}

export function isDisplayReadonlyRuntimeConfig(value: unknown) {
  const runtimeConfig = unwrapApiData(value) as LiveDisplayRuntimeConfig | null
  if (!runtimeConfig || typeof runtimeConfig !== 'object') return false
  if (runtimeConfig.service_role !== 'display_readonly') return false
  return runtimeConfig.display_readonly === undefined || runtimeConfig.display_readonly === true
}

function runtimeConfigParseFailure(url: string, status: number, parseError: string): LiveDisplayBrowserResponse {
  return { url, status, parseError }
}

function parseBoundedContentLength(
  contentLength: string | null,
  maxBytes: number,
): { ok: true } | { ok: false; message: string } {
  if (contentLength === null || contentLength.trim() === '') {
    return { ok: false, message: 'missing content-length for bounded runtime config evidence' }
  }

  const parsed = Number(contentLength)
  if (!Number.isInteger(parsed) || parsed < 0) {
    return { ok: false, message: `invalid content-length for bounded runtime config evidence: ${contentLength}` }
  }
  if (parsed > maxBytes) {
    return {
      ok: false,
      message: `runtime config response body is ${parsed} bytes, exceeding the ${maxBytes} byte evidence limit`,
    }
  }

  return { ok: true }
}

export function createLiveDisplayReadApiEvidence(
  response: Pick<LiveDisplayResponseSource, 'url' | 'status'>,
): LiveDisplayBrowserResponse {
  return { url: response.url(), status: response.status() }
}

export async function parseLiveDisplayRuntimeConfigEvidence(
  response: LiveDisplayResponseSource,
  maxBytes = liveDisplayRuntimeConfigMaxBytes,
): Promise<LiveDisplayBrowserResponse> {
  const url = response.url()
  const status = response.status()
  const contentEncoding = await response.headerValue('content-encoding')
  if (contentEncoding && contentEncoding.trim().toLowerCase() !== 'identity') {
    return runtimeConfigParseFailure(
      url,
      status,
      `runtime config response uses content-encoding ${contentEncoding}; bounded evidence requires identity encoding`,
    )
  }

  const contentLength = parseBoundedContentLength(await response.headerValue('content-length'), maxBytes)
  if (!contentLength.ok) return runtimeConfigParseFailure(url, status, contentLength.message)

  const text = await response.text()
  const actualBytes = new TextEncoder().encode(text).byteLength
  if (actualBytes > maxBytes) {
    return runtimeConfigParseFailure(
      url,
      status,
      `runtime config response body is ${actualBytes} bytes after read, exceeding the ${maxBytes} byte evidence limit`,
    )
  }

  try {
    return { url, status, body: JSON.parse(text) as unknown }
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error)
    return runtimeConfigParseFailure(url, status, `runtime config response is not valid JSON: ${message}`)
  }
}

export function liveDisplayApiBinding(baseURL: string, apiBaseURL: string): LiveDisplayApiBinding {
  const frontend = new URL(baseURL)
  const api = new URL(apiBaseURL)
  const mode: LiveDisplayApiBindingMode = frontend.origin === api.origin ? 'same-origin-proxy' : 'distinct-api'
  return {
    mode,
    expectedOrigin: api.origin,
  }
}

export function isLiveDisplayRuntimeConfigUrl(url: string, binding: LiveDisplayApiBinding) {
  const parsed = new URL(url)
  return parsed.origin === binding.expectedOrigin && parsed.pathname === '/api/v1/runtime/config'
}

export function isLiveDisplayReadApiUrl(url: string, binding: LiveDisplayApiBinding) {
  const parsed = new URL(url)
  return parsed.origin === binding.expectedOrigin && monitoringReadApiPaths.has(parsed.pathname)
}

export function classifyLiveDisplayControlRequest(method: string, url: string) {
  const parsed = new URL(url)
  const normalizedMethod = method.toUpperCase()
  if (parsed.pathname.startsWith('/api/v1/slurm/')) {
    return 'forbidden-slurm-control' as const
  }
  if (
    normalizedMethod !== 'GET' &&
    /^\/api\/v1\/runs\/[^/]+\/(retry|cancel)$/.test(parsed.pathname)
  ) {
    return 'forbidden-run-mutation' as const
  }
  return null
}

export function assertLiveDisplayPageEvidence(evidence: LiveDisplayPageEvidence) {
  if (evidence.permissionDeniedVisible) {
    throw new Error('Live display browser evidence cannot PASS on RBAC 权限不足 page state.')
  }
  if (evidence.runtimeConfigUnavailableVisible) {
    throw new Error('Live display browser evidence cannot PASS when runtime config is unavailable in the page.')
  }
  if (evidence.forbiddenControlRequests.length > 0) {
    throw new Error(
      `Live display browser issued forbidden control requests: ${evidence.forbiddenControlRequests.join(', ')}`,
    )
  }

  const runtimeConfigResponse = evidence.runtimeConfigResponses.find((response) =>
    response.status >= 200 && response.status < 300 && isDisplayReadonlyRuntimeConfig(response.body),
  )
  if (!runtimeConfigResponse) {
    const parseErrors = evidence.runtimeConfigResponses
      .filter((response) => response.parseError)
      .map((response) => `${response.url}: ${response.parseError}`)
      .join('; ')
    throw new Error(
      'Live display browser evidence requires a browser-observed /api/v1/runtime/config response with service_role exactly display_readonly in a bounded JSON body.' +
        (parseErrors ? ` Runtime config parse failures: ${parseErrors}` : ''),
    )
  }

  const readApiResponse = evidence.readApiResponses.find((response) => response.status >= 200 && response.status < 300)
  if (!readApiResponse) {
    throw new Error(
      'Live display browser evidence requires at least one successful browser-observed monitoring read API response from the configured live API.',
    )
  }

  return { runtimeConfigResponse, readApiResponse }
}
