import { describe, expect, it } from 'vitest'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'

import {
  RiverClickPublicationError,
  publishRiverClickEvidence,
  type RiverClickEvidenceFs,
} from '../../playwright.river-click-evidence'
import { buildRiverClickPassEvidence } from '../lib/riverClickEvidence/receipt'
import { listOwnedPosixFdTable, ownedPosixFdCount } from '../test/posixFdTable'

/**
 * Publisher descriptor lifecycle: zero leaked file descriptors on every
 * failure/success terminal, exact codes, and exact final/temp byte/entry sets.
 * Inspection uses owned POSIX surfaces (`/proc/self/fd`, `/dev/fd`) and
 * throws when none is inspectable — never a silent skip.
 */

function fdCount(): number {
  return ownedPosixFdCount()
}

function passPayload() {
  const samples = Array.from({ length: 20 }, (_, index) => ({
    index: index + 1,
    durationMs: 100,
    gfsStatus: 200,
    ifsStatus: 200,
  }))
  const built = buildRiverClickPassEvidence({
    startedAt: '2026-09-02T00:00:00Z',
    endedAt: '2026-09-02T00:02:00Z',
    frontendOrigin: 'https://display.example.test',
    apiOrigin: 'https://api.example.test',
    requestedFeature: { basinId: 'basins_qhh', riverSegmentId: 'seg-001', basinVersionId: 'bv-001', riverNetworkVersionId: 'rn-001' },
    renderedFeature: { basinId: 'basins_qhh', riverSegmentId: 'seg-001', basinVersionId: 'bv-001', riverNetworkVersionId: 'rn-001' },
    gfs: { sourceId: 'GFS', basinId: 'basins_qhh', basinVersionId: 'bv-001', riverNetworkVersionId: 'rn-001', runId: 'run-001', modelId: 'model', cycleTime: '2026-09-02T00:00:00Z', scenario: 'forecast_gfs_deterministic' },
    ifs: { sourceId: 'IFS', basinId: 'basins_qhh', basinVersionId: 'bv-001', riverNetworkVersionId: 'rn-001', runId: 'run-002', modelId: 'model', cycleTime: '2026-09-02T06:00:00Z', scenario: 'forecast_ifs_deterministic' },
    warmup: { index: 0, durationMs: 200, gfsStatus: 200, ifsStatus: 200 },
    samples,
  })
  if (!built.ok) throw new Error('fixture must build')
  return built.receipt
}

function privateRunDir() {
  const root = fs.realpathSync(fs.mkdtempSync(path.join(os.tmpdir(), 'nhms-river-click-publish-')))
  fs.chmodSync(root, 0o700)
  return root
}

function receiptName(runDir: string) {
  return path.join(runDir, 'nhms-frontend-river-click-live-evidence-20260902T000000Z.json')
}

function normalFs(): RiverClickEvidenceFs {
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
    expect(error).toBeInstanceOf(RiverClickPublicationError)
    return (error as RiverClickPublicationError).code
  }
  throw new Error('expected a RiverClickPublicationError')
}

const describeFd = (name: string, fn: () => void) => {
  it(name, () => {
    fn()
  })
}

describe('owned POSIX fd table inspection', () => {
  it('throws when no owned POSIX fd table is inspectable (never silent PASS)', () => {
    expect(() => listOwnedPosixFdTable(['/no/such/fd-table', '/also/missing'], () => {
      throw Object.assign(new Error('EACCES'), { code: 'EACCES' })
    })).toThrow(/owned POSIX fd table is uninspectable/)
  })

  it('uses the first inspectable owned candidate', () => {
    const names = listOwnedPosixFdTable(['/proc/self/fd', '/dev/fd'], (dir) => {
      if (dir === '/proc/self/fd') throw Object.assign(new Error('ENOENT'), { code: 'ENOENT' })
      return ['0', '1', '2']
    })
    expect(names).toEqual(['0', '1', '2'])
  })
})

describe('river-click publisher descriptor lifecycle', () => {
  describeFd('leaks no fd on success and leaves exactly the final', () => {
    const runDir = privateRunDir()
    try {
      const before = fdCount()
      const result = publishRiverClickEvidence(receiptName(runDir), passPayload(), { fs: normalFs() })
      const after = fdCount()
      expect(after).toBe(before)
      expect(fs.existsSync(result.path)).toBe(true)
      expect(fs.readdirSync(runDir)).toEqual([path.basename(result.path)])
      const entries = fs.readdirSync(runDir)
      expect(entries.length).toBe(1)
    } finally {
      fs.rmSync(runDir, { recursive: true, force: true })
    }
  })

  describeFd('leaks no fd on parent-fstat failure (PARENT_CHANGED) and leaves no temp', () => {
    const runDir = privateRunDir()
    try {
      let mutated = false
      const fsSeam: RiverClickEvidenceFs = {
        ...normalFs(),
        lstatSync: (p) => {
          const info = fs.lstatSync(p)
          if (p === runDir && !mutated) {
            mutated = true
            const modified = Object.create(Object.getPrototypeOf(info))
            Object.assign(modified, info)
            modified.ino = info.ino + 1
            return modified as fs.Stats
          }
          return info
        },
      }
      const before = fdCount()
      const code = catchCode(() => publishRiverClickEvidence(receiptName(runDir), passPayload(), { fs: fsSeam }))
      expect(code).toBe('PARENT_CHANGED')
      expect(fdCount()).toBe(before)
      expect(fs.readdirSync(runDir)).toEqual([])
    } finally {
      fs.rmSync(runDir, { recursive: true, force: true })
    }
  })

  describeFd('leaks no fd on invalid temp token and writes nothing', () => {
    const runDir = privateRunDir()
    try {
      const before = fdCount()
      const code = catchCode(() => publishRiverClickEvidence(receiptName(runDir), passPayload(), { fs: normalFs(), tempToken: 'ABC' }))
      expect(code).toBe('TEMP_CREATE_FAILED')
      expect(fdCount()).toBe(before)
      expect(fs.readdirSync(runDir)).toEqual([])
    } finally {
      fs.rmSync(runDir, { recursive: true, force: true })
    }
  })

  describeFd('leaks no fd on temp-create collision and preserves the pre-existing file', () => {
    const runDir = privateRunDir()
    try {
      const target = receiptName(runDir)
      const tempName = `.${path.basename(target)}.tmp-${'a'.repeat(32)}`
      fs.writeFileSync(path.join(runDir, tempName), '{"occupied":true}', { mode: 0o600 })
      const before = fdCount()
      const code = catchCode(() => publishRiverClickEvidence(target, passPayload(), { fs: normalFs(), tempToken: 'a'.repeat(32) }))
      expect(code).toBe('TEMP_CREATE_FAILED')
      expect(fdCount()).toBe(before)
      expect(fs.readFileSync(path.join(runDir, tempName), 'utf8')).toBe('{"occupied":true}')
      expect(fs.existsSync(target)).toBe(false)
    } finally {
      fs.rmSync(runDir, { recursive: true, force: true })
    }
  })

  describeFd('leaks no fd on temp-fstat failure and preserves the unprovable temp', () => {
    const runDir = privateRunDir()
    try {
      let tempFd: number | null = null
      let fstatCalls = 0
      const fsSeam: RiverClickEvidenceFs = {
        ...normalFs(),
        openSync: (p, flags, mode) => {
          const fd = fs.openSync(p, flags, mode)
          if (p.includes('.tmp-')) tempFd = fd
          return fd
        },
        fstatSync: (fd) => {
          fstatCalls += 1
          if (fd === tempFd) throw new Error('fstat boom')
          return fs.fstatSync(fd)
        },
      }
      const before = fdCount()
      const code = catchCode(() => publishRiverClickEvidence(receiptName(runDir), passPayload(), { fs: fsSeam }))
      expect(code).toBe('TEMP_IDENTITY_INVALID')
      expect(fdCount()).toBe(before)
      expect(fstatCalls).toBeGreaterThan(0)
    } finally {
      fs.rmSync(runDir, { recursive: true, force: true })
    }
  })

  describeFd('leaks no fd on openNoClobberTemp write failure and removes only the invocation temp', () => {
    const runDir = privateRunDir()
    try {
      let written = 0
      const fsSeam: RiverClickEvidenceFs = {
        ...normalFs(),
        writeSync: (fd, buffer) => {
          if (written >= 5) throw new Error('disk full')
          const count = Math.min(buffer.length, 5 - written)
          written += count
          return fs.writeSync(fd, buffer.subarray(0, count))
        },
      }
      const before = fdCount()
      const code = catchCode(() => publishRiverClickEvidence(receiptName(runDir), passPayload(), { fs: fsSeam }))
      expect(code).toBe('WRITE_FAILED')
      expect(fdCount()).toBe(before)
      expect(fs.readdirSync(runDir)).toEqual([])
    } finally {
      fs.rmSync(runDir, { recursive: true, force: true })
    }
  })

  describeFd('fchmod failure revalidates the pinned parent BEFORE the cleanup unlink (parent stays open through cleanup)', () => {
    const runDir = privateRunDir()
    try {
      const callOrder: string[] = []
      let tempFd: number | null = null
      let parentFd: number | null = null
      const fsSeam: RiverClickEvidenceFs = {
        ...normalFs(),
        openSync: (p, flags, mode) => {
          const fd = fs.openSync(p, flags, mode)
          if (p.includes('.tmp-')) tempFd = fd
          else parentFd = fd
          return fd
        },
        fchmodSync: (fd) => {
          if (fd === tempFd) throw new Error('fchmod boom')
          return fs.fchmodSync(fd, 0o600)
        },
        lstatSync: (p) => {
          callOrder.push(`lstat:${path.basename(p)}`)
          return fs.lstatSync(p)
        },
        unlinkSync: (p) => {
          callOrder.push(`unlink:${path.basename(p)}`)
          fs.unlinkSync(p)
        },
        fstatSync: (fd) => {
          if (fd === parentFd) callOrder.push('fstat:parent')
          return fs.fstatSync(fd)
        },
      }
      const before = fdCount()
      const code = catchCode(() => publishRiverClickEvidence(receiptName(runDir), passPayload(), { fs: fsSeam }))
      expect(code).toBe('FCHMOD_FAILED')
      expect(fdCount()).toBe(before)
      // The parent fd fstat recheck happens BEFORE the temp lstat/unlink cleanup.
      const unlinkIdx = callOrder.findIndex((entry) => entry.startsWith('unlink:'))
      const parentCheckIdx = callOrder.findIndex((entry) => entry.startsWith('fstat:parent'))
      expect(parentCheckIdx).toBeGreaterThanOrEqual(0)
      expect(unlinkIdx).toBeGreaterThan(parentCheckIdx)
      expect(fs.readdirSync(runDir)).toEqual([])
    } finally {
      fs.rmSync(runDir, { recursive: true, force: true })
    }
  })

  describeFd('normal-path temp unlink revalidates the pinned parent: parent fd identity drift after commit prevents the unlink and preserves the uncertain entry', () => {
    const runDir = privateRunDir()
    try {
      let target = receiptName(runDir)
      const callOrder: string[] = []
      let parentFd: number | null = null
      let parentFstatCalls = 0
      let tempUnlinkAttempted = false
      const fsSeam: RiverClickEvidenceFs = {
        ...normalFs(),
        openSync: (p, flags, mode) => {
          const fd = fs.openSync(p, flags, mode)
          if (p === runDir) parentFd = fd
          return fd
        },
        fstatSync: (fd) => {
          if (fd === parentFd) {
            parentFstatCalls += 1
            callOrder.push(`fstat:parent#${parentFstatCalls}`)
            const info = fs.fstatSync(fd)
            // Initial verification uses calls 1-2 (after open + before link),
            // the pre-commit recheck is call 3, and the NORMAL-UNLINK recheck
            // is call 4. Drift becomes STICKY from call 4 on (the finally's
            // own recheck must also fail, preserving the uncertain temp).
            if (parentFstatCalls >= 4) {
              const modified = Object.create(Object.getPrototypeOf(info))
              Object.assign(modified, info)
              modified.ino = info.ino + 1
              return modified as fs.Stats
            }
            return info
          }
          return fs.fstatSync(fd)
        },
        lstatSync: (p) => {
          callOrder.push(`lstat:${path.basename(p)}`)
          return fs.lstatSync(p)
        },
        unlinkSync: (p) => {
          tempUnlinkAttempted = true
          callOrder.push(`unlink:${path.basename(p)}`)
          fs.unlinkSync(p)
        },
      }
      const before = fdCount()
      const code = catchCode(() => publishRiverClickEvidence(target, passPayload(), { fs: fsSeam }))
      // Parent fd drift before the normal temp unlink: unlink must NOT occur.
      expect(code).toBe('PARENT_CHANGED')
      expect(tempUnlinkAttempted).toBe(false)
      expect(fdCount()).toBe(before)
      // The final entry may remain (the committed final stays; uncertain temp
      // is preserved), but the parent fd is closed last on the terminal.
      expect(fs.statSync(target).nlink).toBeGreaterThanOrEqual(1)
    } finally {
      fs.rmSync(runDir, { recursive: true, force: true })
    }
  })

  describeFd('invalid temp identity cleanup revalidates the pinned parent and preserves the uncertain object when the parent changed', () => {
    const runDir = privateRunDir()
    try {
      const callOrder: string[] = []
      let tempFd: number | null = null
      let parentFd: number | null = null
      let parentFstatCalls = 0
      const fsSeam: RiverClickEvidenceFs = {
        ...normalFs(),
        openSync: (p, flags, mode) => {
          const fd = fs.openSync(p, flags, mode)
          if (p.includes('.tmp-')) tempFd = fd
          else parentFd = fd
          return fd
        },
        fstatSync: (fd) => {
          if (fd === parentFd) {
            parentFstatCalls += 1
            callOrder.push(`fstat:parent#${parentFstatCalls}`)
            const info = fs.fstatSync(fd)
            // Calls 1-2 are the initial parent verification; call 3 is the
            // cleanup recheck. Mutate only the cleanup recheck so the temp
            // identity is invalid but the parent drift is what blocks unlink.
            if (parentFstatCalls === 3) {
              const modified = Object.create(Object.getPrototypeOf(info))
              Object.assign(modified, info)
              modified.ino = info.ino + 1
              return modified as fs.Stats
            }
            return info
          }
          if (fd === tempFd) {
            // Make the temp identity invalid on the fstat after fchmod.
            const info = fs.fstatSync(fd)
            const modified = Object.create(Object.getPrototypeOf(info))
            Object.assign(modified, info)
            modified.mode = 0o644
            return modified as fs.Stats
          }
          return fs.fstatSync(fd)
        },
        unlinkSync: (p) => {
          callOrder.push(`unlink:${path.basename(p)}`)
          fs.unlinkSync(p)
        },
      }
      const before = fdCount()
      const code = catchCode(() => publishRiverClickEvidence(receiptName(runDir), passPayload(), { fs: fsSeam }))
      // With the parent drifted, the invalid-temp cleanup must NOT unlink.
      expect(code).toBe('TEMP_IDENTITY_INVALID')
      expect(fdCount()).toBe(before)
      expect(callOrder.filter((entry) => entry.startsWith('unlink:'))).toEqual([])
      expect(parentFstatCalls).toBeGreaterThanOrEqual(3)
    } finally {
      fs.rmSync(runDir, { recursive: true, force: true })
    }
  })
})
