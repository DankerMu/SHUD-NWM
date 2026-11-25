import { describe, expect, it } from 'vitest'

import { sanitizeHydroMetMessage } from '@/lib/hydroMet/runtime'

describe('sanitizeHydroMetMessage', () => {
  it('redacts credentials, query strings, fragments, file URIs and local paths while keeping the error label', () => {
    const unsafeMessage =
      'ERR_QHH failed opening s3://key:secret@bucket/private?token=abc#frag from file:///volume/data/nwm/Basins/qhh?sig=x#frag and /volume/data/nwm/Basins/qhh plus C:\\nwm\\Basins\\qhh'

    const rendered = sanitizeHydroMetMessage(unsafeMessage)

    expect(rendered).toContain('ERR_QHH')
    expect(rendered).toContain('s3://bucket/private')
    expect(rendered).not.toContain('key:secret')
    expect(rendered).not.toContain('token=abc')
    expect(rendered).not.toContain('#frag')
    expect(rendered).not.toContain('file://')
    expect(rendered).not.toContain('/volume/data/nwm/Basins/qhh')
    expect(rendered).not.toContain('C:\\nwm\\Basins\\qhh')
  })
})
