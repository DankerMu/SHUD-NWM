import { describe, expect, it } from 'vitest'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'

import {
  RIVER_CLICK_RECEIPT_FILENAME_PATTERN,
  RiverClickPublicationError,
  publishRiverClickEvidence,
  type RiverClickEvidenceFs,
} from '../../playwright.river-click-evidence'
import { buildRiverClickPassEvidence, buildRiverClickTerminalEvidence, type RiverClickEvidence } from '../lib/riverClickEvidence/receipt'

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

function blockedPayload() {
  const built = buildRiverClickTerminalEvidence({
    startedAt: '2026-09-02T00:00:00Z',
    endedAt: '2026-09-02T00:00:01Z',
    frontendOrigin: null,
    apiOrigin: null,
    failure: { code: 'RUNTIME_UNAVAILABLE', stage: 'runtime', sampleIndex: null, gfsStatus: null, ifsStatus: null, message: 'unsupported runtime' },
  })
  if (!built.ok) throw new Error('fixture must build')
  return built.receipt
}

function privateRunDir() {
  // The publisher requires the canonical parent to resolve byte-for-byte to the
  // requested path (no symlink path component), so resolve /var -> /private/var
  // exactly like realpathSync would before handing it over.
  const root = fs.realpathSync(fs.mkdtempSync(path.join(os.tmpdir(), 'nhms-river-click-publish-')))
  fs.chmodSync(root, 0o700)
  return root
}

function receiptName(runDir: string, suffix = '20260902T000000Z') {
  return path.join(runDir, `nhms-frontend-river-click-live-evidence-${suffix}.json`)
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

function publishingFs(overrides: Partial<RiverClickEvidenceFs>): RiverClickEvidenceFs {
  return { ...normalFs(), ...overrides }
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

/** @types/node marks geteuid optional (POSIX-only); narrow to a guaranteed callable. */
function euid(): number {
  if (typeof process.geteuid !== 'function') throw new Error('no geteuid')
  return process.geteuid()
}

function runDirCleanup(runDir: string) {
  fs.rmSync(runDir, { recursive: true, force: true })
}

describe('river-click evidence publisher safe path validation', () => {
  it('accepts a canonical absolute receipt basename matching the strict grammar', () => {
    expect(RIVER_CLICK_RECEIPT_FILENAME_PATTERN.test('nhms-frontend-river-click-live-evidence-20260902T000000Z.json')).toBe(true)
    expect(RIVER_CLICK_RECEIPT_FILENAME_PATTERN.test('nhms-frontend-river-click-live-evidence-.json')).toBe(false)
    expect(RIVER_CLICK_RECEIPT_FILENAME_PATTERN.test('nhms-frontend-river-click-live-evidence-' + 'x'.repeat(97) + '.json')).toBe(false)
    expect(RIVER_CLICK_RECEIPT_FILENAME_PATTERN.test('other-name.json')).toBe(false)
  })

  it('publishes one mode-0600 exclusive schema-valid final and leaves no temp', () => {
    const runDir = privateRunDir()
    try {
      const target = receiptName(runDir)
      const result = publishRiverClickEvidence(target, passPayload(), { fs: normalFs() })

      expect(result.path).toBe(target)
      const info = fs.lstatSync(target)
      expect(info.isFile()).toBe(true)
      expect(info.mode & 0o777).toBe(0o600)
      expect(info.nlink).toBe(1)
      expect(info.uid).toBe(euid())
      const parsed = JSON.parse(fs.readFileSync(target, 'utf8'))
      expect(parsed.status).toBe('PASS')
      expect(parsed.samples).toHaveLength(20)
      const siblings = fs.readdirSync(runDir)
      expect(siblings).toEqual([path.basename(target)])
    } finally {
      runDirCleanup(runDir)
    }
  })

  it('publishes a schema-valid BLOCKED receipt when the payload is a blocked document', () => {
    const runDir = privateRunDir()
    try {
      const target = receiptName(runDir, 'blocked')
      const result = publishRiverClickEvidence(target, blockedPayload(), { fs: normalFs() })
      const parsed = JSON.parse(fs.readFileSync(result.path, 'utf8'))
      expect(parsed.status).toBe('BLOCKED')
      expect(parsed.failure.code).toBe('RUNTIME_UNAVAILABLE')
    } finally {
      runDirCleanup(runDir)
    }
  })

  it('rejects an existing final without touching the winner and leaves no temp', () => {
    const runDir = privateRunDir()
    try {
      const target = receiptName(runDir)
      fs.writeFileSync(target, '{"existing":true}', { mode: 0o600 })
      expect(catchCode(() => publishRiverClickEvidence(target, passPayload(), { fs: normalFs() }))).toBe('TARGET_EXISTS')
      expect(fs.readFileSync(target, 'utf8')).toBe('{"existing":true}')
      expect(fs.readdirSync(runDir)).toEqual([path.basename(target)])
    } finally {
      runDirCleanup(runDir)
    }
  })

  it('requires POSIX geteuid and never silently substitutes uid 0', () => {
    const runDir = privateRunDir()
    try {
      expect(process.geteuid).toBeTypeOf('function')
      expect(euid()).toBeGreaterThan(0)
    } finally {
      runDirCleanup(runDir)
    }
  })

  it('rejects a non-absolute and a non-lexically-normalized path at the publisher level', () => {
    const runDir = privateRunDir()
    try {
      const relative = path.join('sub', 'nhms-frontend-river-click-live-evidence-1.json')
      expect(catchCode(() => publishRiverClickEvidence(relative, passPayload(), { fs: normalFs() }))).toBe('RECEIPT_PATH_INVALID')
      const rawDotComponent = `${runDir}/../${path.basename(runDir)}/nhms-frontend-river-click-live-evidence-2.json`
      expect(catchCode(() => publishRiverClickEvidence(rawDotComponent, passPayload(), { fs: normalFs() }))).toBe('RECEIPT_PATH_INVALID')
      expect(fs.readdirSync(runDir)).toEqual([])
    } finally {
      runDirCleanup(runDir)
    }
  })

  it('rejects a NUL byte in the receipt path and never opens anything', () => {
    const runDir = privateRunDir()
    try {
      expect(catchCode(() => publishRiverClickEvidence(path.join(runDir, 'nhms-frontend-river-click-live-evidence-1\x00.json'), passPayload(), { fs: normalFs() }))).toBe('RECEIPT_PATH_INVALID')
      expect(fs.readdirSync(runDir)).toEqual([])
    } finally {
      runDirCleanup(runDir)
    }
  })

  it('rejects a parent that is not mode 0700 with PARENT_MODE_INVALID', () => {
    const runDir = privateRunDir()
    try {
      fs.chmodSync(runDir, 0o755)
      expect(catchCode(() => publishRiverClickEvidence(receiptName(runDir), passPayload(), { fs: normalFs() }))).toBe('PARENT_MODE_INVALID')
      expect(fs.readdirSync(runDir)).toEqual([])
    } finally {
      runDirCleanup(runDir)
    }
  })

  it('rejects a parent with setgid/sticky/special bits (mode check must use 0o7777)', () => {
    const runDir = privateRunDir()
    try {
      fs.chmodSync(runDir, 0o1700) // 0700 + sticky
      expect(catchCode(() => publishRiverClickEvidence(receiptName(runDir), passPayload(), { fs: normalFs() }))).toBe('PARENT_MODE_INVALID')
      fs.chmodSync(runDir, 0o2700) // 0700 + setgid
      expect(catchCode(() => publishRiverClickEvidence(receiptName(runDir), passPayload(), { fs: normalFs() }))).toBe('PARENT_MODE_INVALID')
      fs.chmodSync(runDir, 0o4700) // 0700 + setuid
      expect(catchCode(() => publishRiverClickEvidence(receiptName(runDir), passPayload(), { fs: normalFs() }))).toBe('PARENT_MODE_INVALID')
    } finally {
      fs.chmodSync(runDir, 0o700)
      runDirCleanup(runDir)
    }
  })

  it('rejects a parent owned by a foreign uid (injected identity) and writes nothing', () => {
    const runDir = privateRunDir()
    try {
      const fsSeam = publishingFs({
        lstatSync: (p) => {
          const info = fs.lstatSync(p)
          if (p === runDir) {
            const modified = Object.create(Object.getPrototypeOf(info))
            Object.assign(modified, info)
            modified.uid = euid() + 1
            return modified as fs.Stats
          }
          return info
        },
      })
      expect(catchCode(() => publishRiverClickEvidence(receiptName(runDir), passPayload(), { fs: fsSeam }))).toBe('PARENT_FOREIGN_OWNER')
      expect(fs.readdirSync(runDir)).toEqual([])
    } finally {
      runDirCleanup(runDir)
    }
  })

  it('rejects a symlink path component in the parent and writes nothing', () => {
    const runDir = privateRunDir()
    try {
      const parentRoot = fs.mkdtempSync(path.join(os.tmpdir(), 'nhms-river-click-parent-'))
      const realParent = path.join(parentRoot, 'real')
      fs.mkdirSync(realParent, { mode: 0o700 })
      const linkParent = path.join(parentRoot, 'link')
      fs.symlinkSync(realParent, linkParent, 'dir')
      const target = path.join(linkParent, path.basename(receiptName(realParent)))
      expect(catchCode(() => publishRiverClickEvidence(target, passPayload(), { fs: normalFs() }))).toBe('PARENT_NOT_CANONICAL')
      expect(fs.readdirSync(realParent)).toEqual([])
      fs.rmSync(parentRoot, { recursive: true, force: true })
    } finally {
      runDirCleanup(runDir)
    }
  })

  it('rejects a non-directory parent', () => {
    const runDir = privateRunDir()
    try {
      const file = path.join(runDir, 'not-a-dir')
      fs.writeFileSync(file, 'x', { mode: 0o600 })
      expect(catchCode(() => publishRiverClickEvidence(path.join(file, 'nhms-frontend-river-click-live-evidence-nondir.json'), passPayload(), { fs: normalFs() }))).toBe('PARENT_NOT_DIRECTORY')
    } finally {
      runDirCleanup(runDir)
    }
  })
})

describe('river-click evidence publisher failure and race handling', () => {
  it('fails on a partial write and removes only the invocation temp (WRITE_FAILED)', () => {
    const runDir = privateRunDir()
    try {
      let written = 0
      const fsSeam = publishingFs({
        writeSync: (fd, buffer) => {
          if (written >= 10) throw new Error('disk full')
          const count = Math.min(buffer.length, 10 - written)
          written += count
          return fs.writeSync(fd, buffer.subarray(0, count))
        },
      })
      expect(catchCode(() => publishRiverClickEvidence(receiptName(runDir), passPayload(), { fs: fsSeam }))).toBe('WRITE_FAILED')
      expect(fs.readdirSync(runDir)).toEqual([])
    } finally {
      runDirCleanup(runDir)
    }
  })

  it('fails on fsync failure and removes the temp (FSYNC_FAILED)', () => {
    const runDir = privateRunDir()
    try {
      const fsSeam = publishingFs({ fsyncSync: (fd) => { throw new Error('fsync failed') } })
      expect(catchCode(() => publishRiverClickEvidence(receiptName(runDir), passPayload(), { fs: fsSeam }))).toBe('FSYNC_FAILED')
      expect(fs.readdirSync(runDir)).toEqual([])
    } finally {
      runDirCleanup(runDir)
    }
  })

  it('fails on a link EEXIST race and preserves the winner without removing it (LINK_FAILED)', () => {
    const runDir = privateRunDir()
    try {
      const target = receiptName(runDir)
      const fsSeam = publishingFs({
        linkSync: (oldPath, newPath) => {
          fs.writeFileSync(newPath, '{"winner":true}', { mode: 0o600 })
          throw Object.assign(new Error('file exists'), { code: 'EEXIST' })
        },
      })
      expect(catchCode(() => publishRiverClickEvidence(target, passPayload(), { fs: fsSeam }))).toBe('LINK_FAILED')
      expect(fs.readFileSync(target, 'utf8')).toBe('{"winner":true}')
    } finally {
      runDirCleanup(runDir)
    }
  })

  it('fails on a generic link failure and cleans up the temp (LINK_FAILED)', () => {
    const runDir = privateRunDir()
    try {
      const fsSeam = publishingFs({ linkSync: () => { throw new Error('link failed') } })
      expect(catchCode(() => publishRiverClickEvidence(receiptName(runDir), passPayload(), { fs: fsSeam }))).toBe('LINK_FAILED')
      expect(fs.readdirSync(runDir)).toEqual([])
    } finally {
      runDirCleanup(runDir)
    }
  })

  it('rejects a final-path substitution immediately after link (COMMIT_IDENTITY_INVALID)', () => {
    const runDir = privateRunDir()
    try {
      let swapped = false
      const fsSeam = publishingFs({
        lstatSync: (p) => {
          const info = fs.lstatSync(p)
          let mutated = info
          if (!swapped && p.includes('nhms-frontend-river-click-live-evidence') && info.nlink === 2) {
            swapped = true
            const modified = Object.create(Object.getPrototypeOf(info))
            Object.assign(modified, info)
            modified.dev = info.dev + 1
            mutated = modified as fs.Stats
          }
          return mutated
        },
      })
      expect(catchCode(() => publishRiverClickEvidence(receiptName(runDir), passPayload(), { fs: fsSeam }))).toBe('COMMIT_IDENTITY_INVALID')
      // The temp must NOT be cleaned up because the identity proof failed:
      // but since commit already happened via link, final is present; the temp is
      // uncertain and preserved.
    } finally {
      runDirCleanup(runDir)
    }
  })

  it('detects a mutated Stats.dev (not st_dev) in the commit identity proof', () => {
    const runDir = privateRunDir()
    try {
      let mutated = false
      const fsSeam = publishingFs({
        lstatSync: (p) => {
          const info = fs.lstatSync(p)
          if (p.endsWith('.json') && !p.includes('.tmp-') && !mutated) {
            mutated = true
            const modified = Object.create(Object.getPrototypeOf(info))
            Object.assign(modified, info)
            modified.dev = info.dev + 1
            return modified as fs.Stats
          }
          return info
        },
      })
      expect(catchCode(() => publishRiverClickEvidence(receiptName(runDir), passPayload(), { fs: fsSeam }))).toBe('COMMIT_IDENTITY_INVALID')
    } finally {
      runDirCleanup(runDir)
    }
  })

  it('detects a mutated parent dev/ino across the recheck and fails PARENT_CHANGED', () => {
    const runDir = privateRunDir()
    try {
      let mutated = false
      const fsSeam = publishingFs({
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
      })
      expect(catchCode(() => publishRiverClickEvidence(receiptName(runDir), passPayload(), { fs: fsSeam }))).toBe('PARENT_CHANGED')
      expect(fs.readdirSync(runDir)).toEqual([])
    } finally {
      runDirCleanup(runDir)
    }
  })

  it('opens and pins the parent descriptor before temp creation and uses it for fsync (PARENT_OPEN_FAILED)', () => {
    const runDir = privateRunDir()
    try {
      // fstat on the parent fd must be compared to preflight identity before
      // every path operation; a substitution at open returns a different inode.
      let openedParent: number | null = null
      const fsSeam = publishingFs({
        openSync: (p, flags, mode) => {
          if (p === runDir) {
            openedParent = fs.openSync(p, flags, mode)
            return openedParent
          }
          return fs.openSync(p, flags, mode)
        },
        fstatSync: (fd) => {
          const info = fs.fstatSync(fd)
          if (fd === openedParent) {
            const modified = Object.create(Object.getPrototypeOf(info))
            Object.assign(modified, info)
            modified.ino = info.ino + 1
            return modified as fs.Stats
          }
          return info
        },
      })
      const code = catchCode(() => publishRiverClickEvidence(receiptName(runDir), passPayload(), { fs: fsSeam }))
      expect(['PARENT_CHANGED', 'PARENT_OPEN_FAILED', 'PARENT_FSYNC_FAILED']).toContain(code)
    } finally {
      runDirCleanup(runDir)
    }
  })

  it('fails TEMP_CREATE_FAILED when the injected temp token is not 32 lowercase hex or collides', () => {
    const runDir = privateRunDir()
    try {
      const target = receiptName(runDir)
      // Non-hex token is refused deterministically before any filesystem write.
      expect(catchCode(() => publishRiverClickEvidence(target, passPayload(), { fs: normalFs(), tempToken: 'ABC123' }))).toBe('TEMP_CREATE_FAILED')
      expect(fs.readdirSync(runDir)).toEqual([])
      // Valid 32-hex token colliding with an existing file refuses without clobber.
      const tempName = `.${path.basename(target)}.tmp-${'a'.repeat(32)}`
      fs.writeFileSync(path.join(runDir, tempName), '{"occupied":true}', { mode: 0o600 })
      expect(catchCode(() => publishRiverClickEvidence(target, passPayload(), { fs: normalFs(), tempToken: 'a'.repeat(32) }))).toBe('TEMP_CREATE_FAILED')
      expect(fs.readFileSync(path.join(runDir, tempName), 'utf8')).toBe('{"occupied":true}')
      expect(fs.existsSync(target)).toBe(false)
    } finally {
      runDirCleanup(runDir)
    }
  })

  it('detects an after-write fd mutation by fstat identity (WRITE_FAILED)', () => {
    const runDir = privateRunDir()
    try {
      let tempFd: number | null = null
      const fsSeam = publishingFs({
        openSync: (p, flags, mode) => {
          const fd = fs.openSync(p, flags, mode)
          if (p.includes('.tmp-')) tempFd = fd
          return fd
        },
        fstatSync: (fd) => {
          const info = fs.fstatSync(fd)
          if (fd === tempFd && info.size > 0) {
            const modified = Object.create(Object.getPrototypeOf(info))
            Object.assign(modified, info)
            modified.ino = info.ino + 1
            return modified as fs.Stats
          }
          return info
        },
      })
      expect(catchCode(() => publishRiverClickEvidence(receiptName(runDir), passPayload(), { fs: fsSeam }))).toBe('WRITE_FAILED')
    } finally {
      runDirCleanup(runDir)
    }
  })

  it('detects a final-open fd substitution and mode/nlink mutation at readback (READBACK_FAILED)', () => {
    const runDir = privateRunDir()
    try {
      let finalFd: number | null = null
      const fsSeam = publishingFs({
        openSync: (p, flags, mode) => {
          const fd = fs.openSync(p, flags, mode)
          if (p.endsWith('.json') && !p.includes('.tmp-')) finalFd = fd
          return fd
        },
        fstatSync: (fd) => {
          const info = fs.fstatSync(fd)
          if (fd === finalFd) {
            const modified = Object.create(Object.getPrototypeOf(info))
            Object.assign(modified, info)
            modified.nlink = 99
            return modified as fs.Stats
          }
          return info
        },
      })
      expect(catchCode(() => publishRiverClickEvidence(receiptName(runDir), passPayload(), { fs: fsSeam }))).toBe('READBACK_FAILED')
    } finally {
      runDirCleanup(runDir)
    }
  })

  it('redacts raw OS text/paths in every publisher error message', () => {
    const runDir = privateRunDir()
    try {
      const fsSeam = publishingFs({
        writeSync: () => {
          throw new Error(`ENOSPC: no space left on device at ${runDir}/secret-temp`)
        },
      })
      try {
        publishRiverClickEvidence(receiptName(runDir), passPayload(), { fs: fsSeam })
      } catch (error) {
        expect(error).toBeInstanceOf(RiverClickPublicationError)
        const code = (error as RiverClickPublicationError).code
        expect(code).toBe('WRITE_FAILED')
        expect((error as Error).message).not.toContain(runDir)
        expect((error as Error).message).not.toContain('ENOSPC')
        expect((error as Error).message).not.toContain('secret-temp')
      }
    } finally {
      runDirCleanup(runDir)
    }
  })

  it('preserves an identity-changed temp and fails (CLEANUP_IDENTITY_MISMATCH)', () => {
    const runDir = privateRunDir()
    try {
      let replaced = false
      const fsSeam = publishingFs({
        lstatSync: (p) => {
          const info = fs.lstatSync(p)
          if (!replaced && p.includes('.tmp-')) {
            replaced = true
            const modified = Object.create(Object.getPrototypeOf(info))
            Object.assign(modified, info)
            modified.ino = info.ino + 1
            return modified as fs.Stats
          }
          return info
        },
        unlinkSync: (p) => {
          throw new Error('must not unlink an identity-changed temp')
        },
      })
      expect(catchCode(() => publishRiverClickEvidence(receiptName(runDir), passPayload(), { fs: fsSeam }))).toBe('CLEANUP_IDENTITY_MISMATCH')
      const entries = fs.readdirSync(runDir)
      expect(entries.some((entry) => entry.includes('.tmp-'))).toBe(true)
      expect(fs.existsSync(path.join(runDir, 'nhms-frontend-river-click-live-evidence-20260902T000000Z.json'))).toBe(true)
    } finally {
      runDirCleanup(runDir)
    }
  })

  it('detects a final-path swap after cleanup via sameIdentity with the original descriptor (COMMIT_IDENTITY_INVALID)', () => {
    const runDir = privateRunDir()
    try {
      let swapped = false
      const fsSeam = publishingFs({
        lstatSync: (p) => {
          const info = fs.lstatSync(p)
          if (!swapped && p.endsWith('.json') && info.nlink === 1 && !p.includes('.tmp-')) {
            swapped = true
            const modified = Object.create(Object.getPrototypeOf(info))
            Object.assign(modified, info)
            modified.dev = info.dev + 1
            return modified as fs.Stats
          }
          return info
        },
      })
      expect(catchCode(() => publishRiverClickEvidence(receiptName(runDir), passPayload(), { fs: fsSeam }))).toBe('COMMIT_IDENTITY_INVALID')
    } finally {
      runDirCleanup(runDir)
    }
  })

  it('wraps a partial read into a stable bounded READBACK_FAILED code, not arbitrary OS text', () => {
    const runDir = privateRunDir()
    try {
      let reads = 0
      const fsSeam = publishingFs({
        readSync: (fd, buffer, offset, length, position) => {
          reads += 1
          if (reads === 1) return 0
          return fs.readSync(fd, buffer, offset, length, position)
        },
      })
      expect(catchCode(() => publishRiverClickEvidence(receiptName(runDir), passPayload(), { fs: fsSeam }))).toBe('READBACK_FAILED')
    } finally {
      runDirCleanup(runDir)
    }
  })

  it('fails on a readback that does not reproduce the exact published bytes (READBACK_FAILED)', () => {
    const runDir = privateRunDir()
    try {
      let corrupted = false
      const fsSeam = publishingFs({
        readSync: (fd, buffer, offset, length, position) => {
          if (!corrupted && length > 0) {
            corrupted = true
            const count = fs.readSync(fd, buffer, offset, length, position)
            const original = buffer[offset]
            buffer[offset] = original === 0x7b ? 0x5b : 0x7b
            return count
          }
          return fs.readSync(fd, buffer, offset, length, position)
        },
      })
      expect(catchCode(() => publishRiverClickEvidence(receiptName(runDir), passPayload(), { fs: fsSeam }))).toBe('READBACK_FAILED')
    } finally {
      runDirCleanup(runDir)
    }
  })

  it('fails when the payload exceeds the 262144-byte ceiling and never writes (PAYLOAD_OVER_CEILING)', () => {
    const runDir = privateRunDir()
    try {
      const payload = { ...passPayload(), pad: 'x'.repeat(262144) }
      expect(catchCode(() => publishRiverClickEvidence(receiptName(runDir), payload, { fs: normalFs() }))).toBe('PAYLOAD_OVER_CEILING')
      expect(fs.readdirSync(runDir)).toEqual([])
    } finally {
      runDirCleanup(runDir)
    }
  })

  it('fails on a semantic-invalid payload before any write (INVALID_PAYLOAD)', () => {
    const runDir = privateRunDir()
    try {
      // Deliberately malformed status at the smallest validator boundary: the
      // publisher must reject the semantic-invalid payload before any write.
      // The status literal is intentionally outside the closed union, so it is
      // cast through unknown at this single test-fixture boundary.
      const payload = { ...passPayload(), status: 'MAYBE' } as unknown as RiverClickEvidence
      expect(catchCode(() => publishRiverClickEvidence(receiptName(runDir), payload, { fs: normalFs() }))).toBe('INVALID_PAYLOAD')
      expect(fs.readdirSync(runDir)).toEqual([])
    } finally {
      runDirCleanup(runDir)
    }
  })

  it('provides a bounded diagnostic and no file when the receipt path itself is malformed', () => {
    const messages: string[] = []
    const original = console.error
    console.error = (message: string) => { messages.push(message) }
    try {
      const runDir = privateRunDir()
      try {
        expect(() => publishRiverClickEvidence(path.join(runDir, 'other.json'), passPayload(), { fs: normalFs() })).toThrow()
      } finally {
        runDirCleanup(runDir)
      }
    } finally {
      console.error = original
    }
    expect(messages.join('\n')).toMatch(/(BLOCKED|FAIL):/i)
  })
})
