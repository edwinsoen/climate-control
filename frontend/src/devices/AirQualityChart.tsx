import { useEffect, useId, useState } from 'react'

type Metric = 'pm25' | 'pm1' | 'pm10' | 'air_quality'
type Point = { recorded_at: number; value: number; min: number; max: number; count: number }
type History = {
  source: 'recorded'; start: number; end: number; interval_seconds: number
  metrics: Record<Metric, Point[]>
}
type FetchHistory = (path: string, options?: RequestInit) => Promise<History>
const qualityClasses = ['quality-unknown', 'quality-excellent', 'quality-good', 'quality-fair', 'quality-poor']
const width = 320, height = 180, left = 32, right = 8, top = 44, bottom = 30

function valueLabel(value: number) {
  return `${value.toLocaleString(undefined, { maximumFractionDigits: 2 })} µg/m³`
}

export function AirQualityChart({ deviceId, name, capabilities, fetchHistory }: {
  deviceId: string; name: string; capabilities: string[]; fetchHistory: FetchHistory
}) {
  const [hours, setHours] = useState(24)
  const [history, setHistory] = useState<History | null>(null)
  const [error, setError] = useState('')
  const [active, setActive] = useState<number | null>(null)
  const [expanded, setExpanded] = useState(false)
  const titleId = useId()
  const contentId = useId()
  const gradientId = useId().replace(/:/g, '')
  const hasSensor = capabilities.includes('pm25')

  useEffect(() => {
    if (!hasSensor || !expanded) return
    const controller = new AbortController()
    let pending = false
    setHistory(null); setActive(null); setError('')
    const reload = async () => {
      if (pending) return
      pending = true
      try {
        const data = await fetchHistory(`/api/devices/${deviceId}/history?hours=${hours}`, { signal: controller.signal })
        if (!controller.signal.aborted) { setHistory(data); setError(''); setActive(null) }
      } catch {
        if (!controller.signal.aborted) setError('History could not be loaded.')
      } finally { pending = false }
    }
    void reload()
    const timer = window.setInterval(() => { if (!document.hidden) void reload() }, 60000)
    const visible = () => { if (!document.hidden) void reload() }
    document.addEventListener('visibilitychange', visible)
    return () => { controller.abort(); window.clearInterval(timer); document.removeEventListener('visibilitychange', visible) }
  }, [deviceId, hours, hasSensor, expanded, fetchHistory])

  const points = history?.metrics.pm25 ?? []
  const qualityByTime = new Map(history?.metrics.air_quality.map((point) => [point.recorded_at, point.value]))
  const level = (point: Point) => qualityByTime.get(point.recorded_at) ?? 0
  const qualityClass = (point: Point) => qualityClasses[level(point)] ?? qualityClasses[0]
  const max = Math.max(5, Math.ceil(Math.max(...points.map((point) => point.max), 0) / 5) * 5)
  const min = 0
  const x = (time: number) => left + (time - (history?.start ?? 0)) / ((history?.end ?? 1) - (history?.start ?? 0)) * (width - left - right)
  const y = (value: number) => top + (max - value) / (max - min) * (height - top - bottom)
  const segments: Point[][] = []
  for (const point of points) {
    const segment = segments[segments.length - 1]
    if (!segment || point.recorded_at - segment[segment.length - 1].recorded_at > (history?.interval_seconds ?? 60) * 1.5) segments.push([point])
    else segment.push(point)
  }
  const selected = active === null ? null : points[active]
  const ticks = [0, max / 2, max]
  const dateLabel = (time: number) => new Date(time * 1000).toLocaleString(undefined, hours === 24
    ? { hour: '2-digit', minute: '2-digit' } : { month: 'short', day: 'numeric' })

  function inspectPoint(event: React.PointerEvent<SVGSVGElement>) {
    if (!points.length || event.pointerType === 'touch') return
    const bounds = event.currentTarget.getBoundingClientRect()
    const pointerX = (event.clientX - bounds.left) / bounds.width * width
    let nearest = 0
    for (let index = 1; index < points.length; index++) {
      if (Math.abs(x(points[index].recorded_at) - pointerX) < Math.abs(x(points[nearest].recorded_at) - pointerX)) nearest = index
    }
    setActive(nearest)
  }

  return <section className="air-history" aria-labelledby={titleId}>
    <div className="history-header">
      <span id={titleId} data-testid="history-title">PM2.5 history</span>
      {expanded && <div className="history-periods" data-testid="history-periods" role="group" aria-label={`History period for ${name}`}>
        {[[24, '24h'], [168, '7d'], [720, '30d']].map(([period, label]) => <button key={period} type="button"
          data-testid={`history-period-${label}`} aria-pressed={hours === period} onClick={() => { setHours(Number(period)); setActive(null) }}>{label}</button>)}
      </div>}
      <button type="button" className="history-toggle" data-testid="history-toggle" aria-label={`PM2.5 history for ${name}`} aria-expanded={expanded}
        aria-controls={contentId} onClick={() => { setExpanded(!expanded); setActive(null) }}><span aria-hidden="true" /></button>
    </div>
    <div id={contentId} hidden={!expanded}>
    {expanded && (!hasSensor ? <p className="history-empty">PM2.5 readings unavailable.</p> : <>
      {error && <p className="inline-error" role="status">{error}</p>}
      {!history && !error && <p className="history-empty">Loading history…</p>}
      {history && points.length === 0 && <p className="history-empty">No readings recorded in this period.</p>}
      {history && points.length > 0 && <>
        <svg className="history-chart" data-testid="history-chart" viewBox={`0 0 ${width} ${height}`} role="img"
          aria-label={`PM2.5 history for ${name}. ${points.length} recorded intervals.`}
          onPointerEnter={inspectPoint} onPointerMove={inspectPoint} onPointerLeave={() => setActive(null)}>
          <title>PM2.5 history for {name}</title>
          <defs>{segments.map((segment, index) => <linearGradient key={index} id={`${gradientId}-${index}`} gradientUnits="userSpaceOnUse"
            x1={x(segment[0].recorded_at)} x2={x(segment[segment.length - 1].recorded_at)} y1={0} y2={0}>
            {segment.map((point, pointIndex) => <stop key={pointIndex} offset={segment.length === 1 ? 0 :
              (point.recorded_at - segment[0].recorded_at) / (segment[segment.length - 1].recorded_at - segment[0].recorded_at)} className={`history-gradient-stop ${qualityClass(point)}`} />)}
          </linearGradient>)}</defs>
          {ticks.map((tick) => <g key={tick}>
            <line className="history-grid" x1={left} x2={width - right} y1={y(tick)} y2={y(tick)} />
            <text className="history-value-label" x={left - 6} y={y(tick) + 3}>{tick}</text>
          </g>)}
          {segments.map((segment, index) => <g key={index} data-testid="history-segment">
            <path className="history-range" fill={`url(#${gradientId}-${index})`} d={`M${segment.map((point) => `${x(point.recorded_at)},${y(point.max)}`).join(' L')} L${[...segment].reverse().map((point) => `${x(point.recorded_at)},${y(point.min)}`).join(' L')} Z`} />
            <path className="history-line" stroke={`url(#${gradientId}-${index})`} d={`M${segment.map((point) => `${x(point.recorded_at)},${y(point.value)}`).join(' L')}`} />
            {segment.length === 1 && <circle className={`history-dot ${qualityClass(segment[0])}`} cx={x(segment[0].recorded_at)} cy={y(segment[0].value)} />}
          </g>)}
          {[0, 0.5, 1].map((fraction) => <text key={fraction} x={left + fraction * (width - left - right)} y={height - 8}
            className={`history-time-label ${fraction === 0 ? 'first' : fraction === 1 ? 'last' : ''}`}>{dateLabel(history.start + fraction * (history.end - history.start))}</text>)}
          {active !== null && selected && <g className={`history-selection ${qualityClass(selected)}`} data-testid="history-selection">
            <line className="history-cursor" x1={x(selected.recorded_at)} x2={x(selected.recorded_at)} y1={top} y2={height - bottom} />
            <circle className="history-dot" cx={x(selected.recorded_at)} cy={y(selected.value)} />
            <text className="history-reading" data-testid="history-reading" role="status" x={left} y={22}>
              {new Date(selected.recorded_at * 1000).toLocaleString(undefined, { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' })}
              {' · '}{valueLabel(selected.value)}
            </text>
          </g>}
        </svg>
      </>}
    </>)}
    </div>
  </section>
}
