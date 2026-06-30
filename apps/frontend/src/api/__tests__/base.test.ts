import { describe, expect, it } from 'vitest'

import { buildApiUrl } from '@/api/base'

describe('API base helpers', () => {
  it('resolves backend paths against the configured absolute API base', () => {
    expect(buildApiUrl('/api/v1/runs?source=gfs', 'https://api.example.test/root')).toBe(
      'https://api.example.test/api/v1/runs?source=gfs',
    )
  })

  it('keeps same-origin paths when no API base is configured', () => {
    expect(buildApiUrl('/api/v1/jobs', '')).toBe('/api/v1/jobs')
  })

  it('does not rewrite already absolute URLs', () => {
    expect(buildApiUrl('https://api.example.test/api/v1/jobs', 'https://other.test')).toBe(
      'https://api.example.test/api/v1/jobs',
    )
  })
})
