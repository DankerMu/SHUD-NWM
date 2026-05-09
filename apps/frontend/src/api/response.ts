export interface ApiErrorShape {
  error?: {
    code?: string
    message?: string
    details?: unknown
  }
  message?: string
  status?: string
}

export function getApiErrorMessage(error: unknown, fallback = '请求失败') {
  if (!error) return fallback

  if (error instanceof Error) return error.message || fallback

  if (typeof error === 'object') {
    const apiError = error as ApiErrorShape
    return apiError.error?.message || apiError.message || fallback
  }

  return String(error) || fallback
}

export function unwrapApiData<T>(payload: unknown, fallback = '请求失败'): T {
  if (!payload || typeof payload !== 'object') {
    return payload as T
  }

  const envelope = payload as ApiErrorShape & { data?: unknown }
  if (envelope.status === 'error') {
    throw new Error(getApiErrorMessage(envelope, fallback))
  }

  if ('data' in envelope) {
    return envelope.data as T
  }

  return payload as T
}
