import createClient from 'openapi-fetch'

import { apiBaseUrl } from '@/api/base'
import type { paths } from '@/api/types'

export const client = createClient<paths>({ baseUrl: apiBaseUrl })
