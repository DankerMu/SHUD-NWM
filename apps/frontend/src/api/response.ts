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

  // The error contract is the JSON envelope. openapi-fetch hands back raw response
  // text (proxy HTML pages, plain-text gateway errors) when the body is not JSON;
  // never surface it, or any other primitive, to the UI.
  return fallback
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
