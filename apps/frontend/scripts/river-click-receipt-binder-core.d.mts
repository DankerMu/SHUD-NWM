/**
 * Narrow TypeScript declarations for the Node-20 stdlib binder core.
 * Adjacent to `river-click-receipt-binder-core.mjs`; no ambient wildcard.
 */

export const KNOWN_ARTIFACT: 'nhms-frontend-river-click-live-evidence'
export const KNOWN_SCHEMA_VERSION: '1.0'
export const THRESHOLD_MS: 2000
export const WARMUP_COUNT: 1
export const ACCEPTED_COUNT: 20
export const MAX_RECEIPT_BYTES: 262144

export declare class BinderRefusal extends Error {
  constructor(message: string)
}

export interface BinderFsStats {
  mode: number
  uid: number
  nlink: number
  size: number
  dev: number
  ino: number
  mtimeMs: number
  ctimeMs: number
  isFile?: () => boolean
  isDirectory?: () => boolean
  isSymbolicLink?: () => boolean
}

export interface BinderFileFacts {
  mode: number
  uid: number
  nlink: number
  size: number
  dev: number
  ino: number
  mtimeMs: number
  ctimeMs: number
  mtimeSec: number
  isFile: boolean
  isDir: boolean
  isLink: boolean
}

export interface BinderFs {
  lstatSync(path: string): BinderFsStats
  realpathSync(path: string): string
  openSync(path: string, flags: number): number
  fstatSync(fd: number): BinderFsStats
  readSync(fd: number, buffer: Uint8Array, offset: number, length: number, position: number): number
  closeSync(fd: number): void
  geteuid(): number
}

export function realBinderFs(): BinderFs

export interface BinderArgs {
  receipt?: string
  'frontend-origin'?: string
  'api-origin'?: string
  'basin-id'?: string
  'segment-id'?: string
  'cmd-start'?: string
  'cmd-end'?: string
}

export interface BinderPathnameFactsHook {
  receiptPath: string
  parentPath: string
  parentFacts: BinderFileFacts
  receiptFacts: BinderFileFacts
}

export interface BinderOpenHook {
  fd: number
  receiptPath: string
  parentPath: string
  receiptFacts: BinderFileFacts
}

export interface BinderReadHook {
  fd: number
  receiptPath: string
  parentPath: string
  buffer: Uint8Array
  receiptFacts: BinderFileFacts
}

export interface BinderHooks {
  afterPathnameFacts?: (payload: BinderPathnameFactsHook) => void
  afterOpen?: (payload: BinderOpenHook) => void
  afterRead?: (payload: BinderReadHook) => void
}

export type BinderResult =
  | { ok: true; p95: number }
  | { ok: false; message: string }

export function acceptRiverClickReceipt(
  args: BinderArgs,
  options?: { fs?: BinderFs; hooks?: BinderHooks },
): BinderResult
