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

// MapLibre 瓦片模板：`{z}/{x}/{y}` 占位必须原样保留，且必须是绝对 URL——MapLibre 在 Web Worker
// 内 fetch 瓦片，相对 URL（无 VITE_API_BASE_URL 时）在 worker 里没有 document base，
// `new Request('/api/...')` 抛 "Failed to parse URL"。不能用 new URL 绝对化，会把占位再次百分号编码。
export function buildApiTileUrlTemplate(path: string, baseUrl = apiBaseUrl) {
  const url = buildApiUrl(path, baseUrl)
    .replaceAll('%7Bz%7D', '{z}')
    .replaceAll('%7Bx%7D', '{x}')
    .replaceAll('%7By%7D', '{y}')
  return url.startsWith('/') && typeof window !== 'undefined' && window.location?.origin
    ? `${window.location.origin}${url}`
    : url
}
