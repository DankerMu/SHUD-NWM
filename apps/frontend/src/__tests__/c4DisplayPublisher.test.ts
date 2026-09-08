import { describe, expect, it } from 'vitest'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'

import {
  C4PublicationError,
  C4_RECEIPT_FILENAME_PATTERN,
  publishC4Evidence,
  type C4EvidenceFs,
} from '../../playwright.c4-display-evidence'
import { buildC4PassEvidence, buildC4TerminalEvidence } from '../lib/c4DisplayEvidence/receipt'
import { ownedPosixFdCount } from '../test/posixFdTable'

function passPayload() {
  const built = buildC4PassEvidence({
    startedAt: '2026-09-04T01:00:00Z',
    endedAt: '2026-09-04T01:02:03Z',
    frontendOrigin: 'https://display.example.test',
    apiOrigin: 'https://api.example.test',
    requestedPins: { basinId: 'basins_qhh', riverSegmentId: 'seg-001' },
    gfs: { sourceId: 'GFS', basinId: 'basins_qhh', basinVersionId: 'bv-001', riverNetworkVersionId: 'rn-001', runId: 'run-gfs', modelId: 'model-gfs', cycleTime: '2026-09-04T00:00:00Z', scenario: 'forecast_gfs_deterministic' },
    ifs: { sourceId: 'IFS', basinId: 'basins_qhh', basinVersionId: 'bv-001', riverNetworkVersionId: 'rn-001', runId: 'run-ifs', modelId: 'model-ifs', cycleTime: '2026-09-04T06:00:00Z', scenario: 'forecast_ifs_deterministic' },
    home: { path: '/', mapSurfaceVisible: true, runtimeConfigStatus: 200, runtimeServiceRole: 'display_readonly', currentReadObserved: true, currentReadPath: '/api/v1/basins' },
    opsGfs: { sourceId: 'GFS', path: '/ops', headingObserved: true, permissionDenied: false, runtimeUnavailable: false, statusStatus: 200, stagesStatus: 200, jobsStatus: 200, jobId: 'job-gfs', logsStatus: 200, roleSelectorCount: 0, retryCancelControlCount: 0, slurmRequestCount: 0, nonGetControlCount: 0, queueReadonlyVisible: true, operatorRecoveryVisible: true },
    opsIfs: { sourceId: 'IFS', path: '/ops', headingObserved: true, permissionDenied: false, runtimeUnavailable: false, statusStatus: 200, stagesStatus: 200, jobsStatus: 200, jobId: 'job-ifs', logsStatus: 200, roleSelectorCount: 0, retryCancelControlCount: 0, slurmRequestCount: 0, nonGetControlCount: 0, queueReadonlyVisible: true, operatorRecoveryVisible: true },
    noControl: { slurmRequestCount: 0, nonGetControlCount: 0 },
  })
  if (!built.ok) throw new Error('fixture must build')
  return built.receipt
}

function privateRunDir() {
  const root = fs.realpathSync(fs.mkdtempSync(path.join(os.tmpdir(), 'nhms-c4-publish-')))
  fs.chmodSync(root, 0o700)
  return root
}

function receiptName(runDir: string, suffix = '20260904T000000Z') {
  return path.join(runDir, `nhms-frontend-c4-live-evidence-${suffix}.json`)
}

function normalFs(): C4EvidenceFs {
  return {
    lstatSync: (p) => fs.lstatSync(p),
    statSync: (p) => fs.statSync(p),
    realpathSync: (p) => fs.realpathSync(p),
    openSync: (p, flags, mode) => fs.openSync(p, flags, mode),
    fstatSync: (fd) => fs.fstatSync(fd),
    fchmodSync: (fd, mode) => fs.fchmodSync(fd, mode),
    writeSync: (fd, buffer) => fs.writeSync(fd, buffer),
    fsyncSync: (fd) => fs.fsyncSync(fd),
    closeSync: (fd) => fs.closeSync(fd),
    linkSync: (oldPath, newPath) => fs.linkSync(oldPath, newPath),
    unlinkSync: (p) => fs.unlinkSync(p),
    readSync: (fd, buffer, offset, length, position) => fs.readSync(fd, buffer, offset, length, position),
  }
}

function catchCode(fn: () => unknown): string {
  try {
    fn()
  } catch (error) {
    expect(error).toBeInstanceOf(C4PublicationError)
    return (error as C4PublicationError).code
  }
  throw new Error('expected a C4PublicationError')
}

describe('C4 evidence publisher', () => {
  it('accepts the C4 basename grammar and rejects the river-click basename', () => {
    expect(C4_RECEIPT_FILENAME_PATTERN.test('nhms-frontend-c4-live-evidence-20260904T000000Z.json')).toBe(true)
    expect(C4_RECEIPT_FILENAME_PATTERN.test('nhms-frontend-river-click-live-evidence-20260904T000000Z.json')).toBe(false)
  })

  it('publishes one mode-0600 exclusive schema-valid final and leaves no temp; fd count is stable', () => {
    const runDir = privateRunDir()
    try {
      const target = receiptName(runDir)
      const before = ownedPosixFdCount()
      const result = publishC4Evidence(target, passPayload(), { fs: normalFs() })
      expect(result.path).toBe(target)
      const info = fs.lstatSync(target)
      expect(info.isFile()).toBe(true)
      expect(info.mode & 0o777).toBe(0o600)
      expect(info.nlink).toBe(1)
      expect(fs.readdirSync(runDir)).toEqual([path.basename(target)])
      expect(ownedPosixFdCount()).toBe(before)
    } finally {
      fs.rmSync(runDir, { recursive: true, force: true })
    }
  })

  it('rejects an existing final without touching the winner', () => {
    const runDir = privateRunDir()
    try {
      const target = receiptName(runDir)
      fs.writeFileSync(target, '{"existing":true}', { mode: 0o600 })
      expect(catchCode(() => publishC4Evidence(target, passPayload(), { fs: normalFs() }))).toBe('TARGET_EXISTS')
      expect(fs.readFileSync(target, 'utf8')).toBe('{"existing":true}')
    } finally {
      fs.rmSync(runDir, { recursive: true, force: true })
    }
  })

  it('refuses a parent that is not mode 0700', () => {
    const runDir = privateRunDir()
    try {
      fs.chmodSync(runDir, 0o755)
      expect(catchCode(() => publishC4Evidence(receiptName(runDir), passPayload(), { fs: normalFs() }))).toBe('PARENT_MODE_INVALID')
    } finally {
      fs.chmodSync(runDir, 0o700)
      fs.rmSync(runDir, { recursive: true, force: true })
    }
  })

  it('wraps a native parent-fstat EBADF as C4PublicationError PARENT_CHANGED without temp or fd leak', () => {
    const runDir = privateRunDir()
    try {
      let parentFd: number | null = null
      const fsSeam: C4EvidenceFs = {
        ...normalFs(),
        openSync: (p, flags, mode) => {
          const fd = fs.openSync(p, flags, mode)
          if (p === runDir) parentFd = fd
          return fd
        },
        fstatSync: (fd) => {
          if (fd === parentFd) throw Object.assign(new Error('native EBADF'), { code: 'EBADF' })
          return fs.fstatSync(fd)
        },
      }
      const before = ownedPosixFdCount()
      let thrown: unknown
      try {
        publishC4Evidence(receiptName(runDir), passPayload(), { fs: fsSeam })
      } catch (error) {
        thrown = error
      }
      expect(thrown).toBeInstanceOf(C4PublicationError)
      expect((thrown as C4PublicationError).code).toBe('PARENT_CHANGED')
      expect(ownedPosixFdCount()).toBe(before)
      expect(fs.readdirSync(runDir)).toEqual([])
    } finally {
      fs.rmSync(runDir, { recursive: true, force: true })
    }
  })

  it('does not publish raw C4 publication errors into a terminal receipt', async () => {
    const { publishC4Terminal } = await import('../../playwright.c4-display-terminal')
    const result = publishC4Terminal({
      requestedPins: null,
      gfs: null,
      ifs: null,
      home: null,
      opsGfs: null,
      opsIfs: null,
      noControl: { slurmRequestCount: 0, nonGetControlCount: 1 },
      failure: { code: 'INTERNAL_ERROR', stage: 'runtime', message: 'file:///private/secret' },
    }, {
      startedAt: '2026-09-04T01:00:00Z',
      endedAt: '2026-09-04T01:00:01Z',
      frontendOrigin: 'https://display.example.test',
      apiOrigin: 'https://api.example.test',
      receiptPath: '/private/receipt.json',
    }, {
      publish: () => ({ path: '/private/receipt.json' }),
    })
    expect(result).toMatchObject({ ok: false, code: 'RECEIPT_BUILD_FAILED' })
  })

  it.each([
    ['page error during home proof', 'HOME_NOT_READY', 'home'],
    ['home map source error contradicts readiness', 'HOME_NOT_READY', 'home'],
    ['C4 lane internal error', 'INTERNAL_ERROR', 'runtime'],
    ['whole-run deadline exceeded before preflight', 'WHOLE_RUN_TIMEOUT', 'timeout'],
  ] as const)('publishes exactly one FAIL receipt for fixed terminal message %s', async (message, code, stage) => {
    const { publishC4Terminal } = await import('../../playwright.c4-display-terminal')
    const published: unknown[] = []
    const result = publishC4Terminal({
      requestedPins: null,
      gfs: null,
      ifs: null,
      home: null,
      opsGfs: null,
      opsIfs: null,
      noControl: { slurmRequestCount: 0, nonGetControlCount: 0 },
      failure: { code, stage, message },
    }, {
      startedAt: '2026-09-04T01:00:00Z',
      endedAt: '2026-09-04T01:00:01Z',
      frontendOrigin: 'https://display.example.test',
      apiOrigin: 'https://api.example.test',
      receiptPath: '/private/receipt.json',
    }, {
      publish: (_path, receipt) => {
        published.push(receipt)
        return { path: '/private/receipt.json' }
      },
    })

    expect(result.ok).toBe(true)
    expect(published).toHaveLength(1)
    expect((published[0] as { status: string }).status).toBe('FAIL')
  })

  it('returns a terminal publication failure instead of accepting zero writes', async () => {
    const { publishC4Terminal } = await import('../../playwright.c4-display-terminal')
    const result = publishC4Terminal({
      requestedPins: null,
      gfs: null,
      ifs: null,
      home: null,
      opsGfs: null,
      opsIfs: null,
      noControl: { slurmRequestCount: 0, nonGetControlCount: 0 },
      failure: { code: 'INTERNAL_ERROR', stage: 'runtime', message: 'C4 lane internal error' },
    }, {
      startedAt: '2026-09-04T01:00:00Z',
      endedAt: '2026-09-04T01:00:01Z',
      frontendOrigin: 'https://display.example.test',
      apiOrigin: 'https://api.example.test',
      receiptPath: '/private/receipt.json',
    }, {
      publish: () => { throw new Error('file:///private/receipt') },
    })

    expect(result).toMatchObject({ ok: false, code: 'PUBLICATION_FAILED', message: 'C4 terminal publication failed' })
  })

  it('publishes a FAIL terminal with observed non-GET control counts', async () => {
    const { publishC4Terminal } = await import('../../playwright.c4-display-terminal')
    const published: unknown[] = []
    const result = publishC4Terminal({
      requestedPins: null,
      gfs: null,
      ifs: null,
      home: null,
      opsGfs: null,
      opsIfs: null,
      noControl: { slurmRequestCount: 0, nonGetControlCount: 1 },
      failure: { code: 'FORBIDDEN_CONTROL', stage: 'ops', message: 'observed control request count is non-zero' },
    }, {
      startedAt: '2026-09-04T01:00:00Z',
      endedAt: '2026-09-04T01:00:01Z',
      frontendOrigin: 'https://display.example.test',
      apiOrigin: 'https://api.example.test',
      receiptPath: '/private/receipt.json',
    }, {
      publish: (_path, receipt) => {
        published.push(receipt)
        return { path: '/private/receipt.json' }
      },
    })
    expect(result.ok).toBe(true)
    expect((published[0] as { no_control: { non_get_control_count: number } }).no_control.non_get_control_count).toBe(1)
  })

  it('redacts private publication-path errors from public diagnostics', () => {
    const runDir = privateRunDir()
    try {
      const target = receiptName(runDir)
      fs.writeFileSync(target, '{"existing":true}', { mode: 0o600 })
      let diagnostic = ''
      try {
        publishC4Evidence(target, passPayload(), { fs: normalFs() })
      } catch (error) {
        diagnostic = (error as Error).message
      }
      expect(diagnostic).toContain('TARGET_EXISTS')
      expect(diagnostic).not.toContain(runDir)
      expect(diagnostic).not.toContain(target)
    } finally {
      fs.rmSync(runDir, { recursive: true, force: true })
    }
  })

  it('publishes a schema-valid BLOCKED receipt', () => {
    const runDir = privateRunDir()
    try {
      const built = buildC4TerminalEvidence({
        startedAt: '2026-09-04T01:00:00Z',
        endedAt: '2026-09-04T01:00:01Z',
        frontendOrigin: null,
        apiOrigin: null,
        failure: { code: 'RUNTIME_UNAVAILABLE', stage: 'runtime', message: 'unsupported runtime' },
      })
      if (!built.ok) throw new Error('blocked must build')
      const result = publishC4Evidence(receiptName(runDir, 'blocked'), built.receipt, { fs: normalFs() })
      const parsed = JSON.parse(fs.readFileSync(result.path, 'utf8'))
      expect(parsed.status).toBe('BLOCKED')
    } finally {
      fs.rmSync(runDir, { recursive: true, force: true })
    }
  })
})
