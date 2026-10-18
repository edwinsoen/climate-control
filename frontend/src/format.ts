export function ago(value: string | number | null | undefined): string {
  if (value === null || value === undefined || value === '') return 'No reading yet'
  const time = typeof value === 'number' ? value * 1000 : Date.parse(value)
  const seconds = Math.max(0, Math.floor((Date.now() - time) / 1000))
  if (seconds < 60) return 'Just now'
  if (seconds < 3600) return `${Math.floor(seconds / 60)} min ago`
  if (seconds < 86400) return `${Math.floor(seconds / 3600)} hr ago`
  return `${Math.floor(seconds / 86400)} days ago`
}

/** Clock time for today, or a short date for earlier days. */
export function clock(seconds: number): string {
  const date = new Date(seconds * 1000)
  const today = new Date()
  return date.toDateString() === today.toDateString()
    ? date.toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit', hour12: false })
    : date.toLocaleDateString(undefined, { month: 'short', day: 'numeric' })
}

export function plural(count: number, word: string) {
  return `${count} ${word}${count === 1 ? '' : 's'}`
}
