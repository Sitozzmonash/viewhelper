const pad = (value: number) => String(value).padStart(2, '0')

/** 09:41:12 */
export function formatClock(timestamp: number) {
  const date = new Date(timestamp)
  return `${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`
}

/** 00:08 */
export function formatDuration(seconds: number) {
  return `${pad(Math.floor(seconds / 60))}:${pad(seconds % 60)}`
}

/** 2024-06-10 09:42:15 */
export function formatDateTime(timestamp: number) {
  const date = new Date(timestamp)
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${formatClock(timestamp)}`
}
