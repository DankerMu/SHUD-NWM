export const apiBaseUrl = import.meta.env.VITE_API_BASE_URL ?? ''

export function buildApiUrl(path: string, baseUrl = apiBaseUrl) {
  if (/^https?:\/\//i.test(path)) return path
  const normalizedPath = path.startsWith('/') ? path : `/${path}`
  if (!baseUrl) return normalizedPath
  return new URL(normalizedPath, baseUrl.endsWith('/') ? baseUrl : `${baseUrl}/`).toString()
}

export function apiFetch(input: string, init?: RequestInit) {
  return fetch(buildApiUrl(input), init)
}
