import { describe, expect, it } from 'vitest'

import { getApiErrorMessage } from '@/api/response'

// Characterization test: pins what getApiErrorMessage does today for every error
// shape the frontend actually receives. Expectations come from the input table in
// openspec/changes/entropy-burndown-2026-10/tasks.md (7.4 successor, #2956).
// #2978 (entropy-followups-2-2026-10 task 4.1) made non-Error, non-object values
// fail closed: the raw HTML body and number rows now expect the fallback.
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
  // openapi-fetch hands back the raw text when the error body is not JSON; the
  // contract is the JSON envelope, so raw proxy/gateway text never reaches the UI (#2978).
  ['non-JSON HTML error body -> fallback', '<html>502 Bad Gateway</html>', FALLBACK],
  ['non-JSON plain-text proxy error -> fallback', 'upstream connect error or disconnect/reset before headers', FALLBACK],
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
  ['number primitive -> fallback', 42, FALLBACK],
]

describe('getApiErrorMessage', () => {
  it.each(cases)('%s', (_label, input, expected) => {
    expect(getApiErrorMessage(input, FALLBACK)).toBe(expected)
  })

  it('uses the default fallback when none is given', () => {
    expect(getApiErrorMessage(null)).toBe('请求失败')
  })
})
