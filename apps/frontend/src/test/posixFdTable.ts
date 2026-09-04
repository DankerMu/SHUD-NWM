import fs from 'node:fs'

/** Owned POSIX fd-table surfaces. Linux exposes `/proc/self/fd`; macOS `/dev/fd`. */
export const OWNED_POSIX_FD_TABLE_CANDIDATES = ['/proc/self/fd', '/dev/fd'] as const

export type PosixFdReaddir = (path: string) => string[]

/**
 * Inspect the current process fd table through owned POSIX surfaces.
 * Throws when NONE of the candidates is inspectable. A silent `-1`/skip is not
 * an oracle: leak assertions must fail closed rather than record PASS.
 */
export function listOwnedPosixFdTable(
  candidates: readonly string[] = OWNED_POSIX_FD_TABLE_CANDIDATES,
  readdirSync: PosixFdReaddir = (dir) => fs.readdirSync(dir),
): string[] {
  const tried: string[] = []
  for (const dir of candidates) {
    tried.push(dir)
    try {
      return readdirSync(dir)
    } catch {
      // try the next owned surface
    }
  }
  throw new Error(`owned POSIX fd table is uninspectable (${tried.join(', ')})`)
}

export function ownedPosixFdCount(
  candidates: readonly string[] = OWNED_POSIX_FD_TABLE_CANDIDATES,
  readdirSync: PosixFdReaddir = (dir) => fs.readdirSync(dir),
): number {
  return listOwnedPosixFdTable(candidates, readdirSync).length
}
