import { useEffect, useId, useState } from 'react'
import { PM25_BREAKS } from '../bands'
import type { Api } from '../devices/types'

type Point = { recorded_at: number; value: number }
type History = { start: number; end: number; interval_seconds: number; metrics: { pm25?: Point[] } }
const HEIGHT = 40, WIDTH = 160

export function Sparkline({ api, deviceId, name }: { api: Api; deviceId: string; name: string }) {
  const [history, setHistory] = useState<History | null>(null)
  const gradient = useId().replace(/:/g, '')

  useEffect(() => {
    const controller = new AbortController()
    const load = () => api<History>(`/api/devices/${deviceId}/history?hours=24`, { signal: controller.signal })
      .then(setHistory).catch(() => {})
    void load()
    const timer = window.setInterval(() => { if (!document.hidden) void load() }, 300_000)
    return () => { controller.abort(); window.clearInterval(timer) }
  }, [api, deviceId])

  const points = history?.metrics.pm25 ?? []
  if (!history || points.length < 2) return <div className="sparkline-empty" data-testid="device-sparkline-empty">
    {history ? 'Not enough readings yet' : 'Loading readings…'}
  </div>
  const top = Math.max(PM25_BREAKS[2] + 5, ...points.map((point) => point.value))
  const x = (time: number) => (time - history.start) / Math.max(1, history.end - history.start) * WIDTH
  const y = (value: number) => 2 + (1 - Math.max(0, value) / top) * (HEIGHT - 4)
  const segments: Point[][] = []
  for (const point of points) {
    const segment = segments[segments.length - 1]
    if (!segment || point.recorded_at - segment[segment.length - 1].recorded_at > history.interval_seconds * 1.5) segments.push([point])
    else segment.push(point)
  }
  // Hard color stops at the band boundaries color the line by its band.
  const offsets = PM25_BREAKS.map((limit) => Math.min(1, Math.max(0, y(limit) / HEIGHT))).reverse()
  const bands = ['very-unhealthy', 'unhealthy', 'moderate', 'good']
  const latest = points[points.length - 1].value
  return <svg className="sparkline" data-testid="device-sparkline" viewBox={`0 0 ${WIDTH} ${HEIGHT}`} preserveAspectRatio="none"
    role="img" aria-label={`PM2.5 over the last 24 hours for ${name}, latest ${latest.toLocaleString()} µg/m³`}>
    <defs><linearGradient id={gradient} gradientUnits="userSpaceOnUse" x1="0" y1="0" x2="0" y2={HEIGHT}>
      {bands.flatMap((band, index) => [
        <stop key={band + '-start'} offset={index === 0 ? 0 : offsets[index - 1]} className={`sparkline-stop ${band}`} />,
        <stop key={band + '-end'} offset={index === 3 ? 1 : offsets[index]} className={`sparkline-stop ${band}`} />,
      ])}
    </linearGradient></defs>
    <rect className="sparkline-band" x="0" y={y(PM25_BREAKS[1])} width={WIDTH} height={y(PM25_BREAKS[0]) - y(PM25_BREAKS[1])} />
    {segments.map((segment, index) => <path key={index} className="sparkline-line" stroke={`url(#${gradient})`}
      d={'M' + segment.map((point) => `${x(point.recorded_at).toFixed(1)} ${y(point.value).toFixed(1)}`).join(' L')} />)}
  </svg>
}
