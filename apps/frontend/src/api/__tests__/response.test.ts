import { describe, expect, it } from 'vitest'

import { getApiErrorMessage } from '@/api/response'

// Characterization test: pins what getApiErrorMessage does today for every error
// shape the frontend actually receives. Expectations come from the input table in
// openspec/changes/entropy-burndown-2026-10/tasks.md (7.4 successor, #2956).
const FALLBACK = '回落文案'

const cases: Array<[label: string, input: unknown, expected: string]> = [
  ['null -> fallback', null, FALLBACK],
  ['undefined -> fallback', undefined, FALLBACK],
  ['empty string -> fallback', '', FALLBACK],
  ['zero -> fallback', 0, FALLBACK],
  ['false -> fallback', false, FALLBACK],
  ['Error with message -> message', new Error('x'), 'x'],
  ['Error with empty message -> fallback', new Error(''), FALLBACK],
  // openapi-fetch rethrows the fetch rejection on network failure.
  ['network failure TypeError -> message', new TypeError('Failed to fetch'), 'Failed to fetch'],
  [
    'AbortError DOMException -> message',
    new DOMException('The operation was aborted.', 'AbortError'),
    'The operation was aborted.',
  ],
  // openapi-fetch hands back the raw text when the error body is not JSON.
  ['non-JSON error body -> the raw string', '<html>502 Bad Gateway</html>', '<html>502 Bad Gateway</html>'],
  // Backend envelope, apps/api/errors.py.
  [
    'backend error envelope -> error.message',
    {
      request_id: 'req-1',
      status: 'error',
      error: { code: 'NOT_FOUND', message: 'run not found', details: { run_id: 'r1' } },
    },
    'run not found',
  ],
  ['error.code without message, top-level message -> top-level message', { error: { code: 'C' }, message: 'top' }, 'top'],
  ['error.code without any message -> fallback', { error: { code: 'C' } }, FALLBACK],
  // apps/api/startup_wiring.py: the only non-envelope response.
  ['{detail} non-envelope -> fallback', { detail: 'Not found' }, FALLBACK],
  // Non-zero, non-NaN primitive: 0 / NaN / 0n are caught by the falsy guard instead.
  ['number primitive -> String(e)', 42, '42'],
]

describe('getApiErrorMessage', () => {
  it.each(cases)('%s', (_label, input, expected) => {
    expect(getApiErrorMessage(input, FALLBACK)).toBe(expected)
  })

  it('uses the default fallback when none is given', () => {
    expect(getApiErrorMessage(null)).toBe('请求失败')
  })
})
