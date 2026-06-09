import { existsSync, readdirSync, readFileSync, statSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import path from 'node:path'

export interface LiveDisplayEnv {
  baseURL: string
  apiBaseURL: string
  viteApiBaseURL: string
}

const liveDisplayRequiredEnv = ['PLAYWRIGHT_LIVE_BASE_URL', 'PLAYWRIGHT_LIVE_API_BASE_URL'] as const
const liveDisplaySpecPattern = /(^|[./\\-])live-display\.spec\.ts$/
const broadApiRouteMockPattern = /page\s*\.\s*route\s*\(\s*(['"`])\*\*\/api\/v1\/\*\*\1/g

export function parsePlaywrightWorkers(value: string | undefined) {
  if (value === undefined || value.trim() === '') return 1
  const parsed = Number(value)
  if (!Number.isInteger(parsed) || parsed < 1) {
    throw new Error('PLAYWRIGHT_WORKERS must be a positive integer.')
  }
  return Math.min(parsed, 4)
}

export function isPlaywrightProjectRequested(projectName: string, argv = process.argv) {
  return argv.some((arg, index) => {
    if (arg === `--project=${projectName}`) return true
    if (arg === '--project' && argv[index + 1] === projectName) return true
    return false
  })
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
  const root = pathFromInput(testDir)
  if (!existsSync(root)) return []

  const files: string[] = []
  const visit = (entry: string) => {
    const stat = statSync(entry)
    if (stat.isDirectory()) {
      for (const child of readdirSync(entry)) visit(path.join(entry, child))
      return
    }
    if (stat.isFile() && liveDisplaySpecPattern.test(entry)) files.push(entry)
  }

  visit(root)
  return files.sort()
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
