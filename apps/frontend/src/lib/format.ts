export function formatDate(value: string | number | Date | null | undefined) {
  if (!value) return '-'

  const date = value instanceof Date ? value : new Date(value)
  if (Number.isNaN(date.getTime())) return '-'

  return new Intl.DateTimeFormat('zh-CN', {
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
    hour12: false,
  }).format(date)
}

export function formatDuration(seconds: number | null | undefined) {
  if (seconds === null || seconds === undefined || seconds < 0) return '-'

  const totalSeconds = Math.round(seconds)
  const hours = Math.floor(totalSeconds / 3600)
  const minutes = Math.floor((totalSeconds % 3600) / 60)
  const remainingSeconds = totalSeconds % 60

  if (hours > 0) return `${hours}h ${minutes}m`
  if (minutes > 0) return remainingSeconds ? `${minutes}m ${remainingSeconds}s` : `${minutes}m`
  return `${remainingSeconds}s`
}
