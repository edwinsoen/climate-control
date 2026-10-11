import { useEffect, useId, useRef, useState } from 'react'
import type { ReactNode } from 'react'

type Metric = 'aqi' | 'pm25' | 'pm1' | 'pm10' | 'air_quality'
type Point = { recorded_at: number; value: number; min: number; max: number; count: number }
type Setting = {
  recorded_at: number; end_at: number; power: string | null; mode: string | null
  fan_level: number | null; reported_fan_level: number | null
}
type History = {
  source: 'recorded' | 'aqicn'; start: number; end: number; interval_seconds: number
  metrics: Partial<Record<Metric, Point[]>>
  settings?: Setting[]
}
type FetchHistory = (path: string, options?: RequestInit) => Promise<History>
const qualityClasses = ['quality-unknown', 'quality-excellent', 'quality-good', 'quality-fair', 'quality-poor']
const left = 44

function settingFan(setting: Setting) {
  if (setting.power === 'off') return 0
  return setting.mode === 'manual' ? setting.fan_level : setting.reported_fan_level ?? setting.fan_level
}

function settingClass(setting: Setting) {
  return `setting-${setting.power === 'off' ? 'off' : ['manual', 'auto', 'sleep'].includes(setting.mode ?? '') ? setting.mode : 'unknown'}`
}

function settingLabel(setting: Setting) {
  if (setting.power === 'off') return 'Off'
  const mode = setting.mode ? setting.mode[0].toUpperCase() + setting.mode.slice(1) : 'Mode unknown'
  const fan = settingFan(setting)
  return `${setting.power === 'on' ? 'On' : 'Power unknown'} · ${mode}${fan !== null ? ` · Fan ${fan}` : ''}`
}

function valueLabel(value: number, unit: string) {
  return `${value.toLocaleString(undefined, { maximumFractionDigits: 2 })} ${unit}`
}

export function AirQualityChart({ deviceId, name, capabilities, fetchHistory, metric = 'pm25', historyPath, unit = 'µg/m³', metricSelector }: {
  deviceId: string; name: string; capabilities: string[]; fetchHistory: FetchHistory
  metric?: 'aqi' | 'pm25' | 'pm10'; historyPath?: string; unit?: string
  metricSelector?: ReactNode
}) {
  const [hours, setHours] = useState(24)
  const [history, setHistory] = useState<History | null>(null)
  const [error, setError] = useState('')
  const [active, setActive] = useState<number | null>(null)
  const [activeTime, setActiveTime] = useState<number | null>(null)
  const [expanded, setExpanded] = useState(false)
  const [width, setWidth] = useState(320)
  const content = useRef<HTMLDivElement>(null)
  const titleId = useId()
  const contentId = useId()
  const gradientId = useId().replace(/:/g, '')
  const hasSensor = capabilities.includes(metric)
  const canFetchHistory = hasSensor || !historyPath
  const metricLabel = metric === 'aqi' ? 'AQI' : `${metric === 'pm25' ? 'PM2.5' : 'PM10'}${unit === 'AQI' ? ' AQI' : ''}`
  const historyLabel = historyPath ? metricLabel : hasSensor ? `${metricLabel} & settings` : 'Settings'

  useEffect(() => {
    if (!content.current) return
    const observer = new ResizeObserver(([entry]) => {
      if (entry.contentRect.width > 0) setWidth(Math.max(240, Math.round(entry.contentRect.width)))
    })
    observer.observe(content.current)
    return () => observer.disconnect()
  }, [])

  useEffect(() => {
    if (!canFetchHistory || !expanded) return
    const controller = new AbortController()
    let pending = false
    setHistory(null); setActive(null); setActiveTime(null); setError('')
    const reload = async () => {
      if (pending) return
      pending = true
      try {
        const data = await fetchHistory(`${historyPath ?? `/api/devices/${deviceId}/history`}?hours=${hours}`, { signal: controller.signal })
        if (!controller.signal.aborted) { setHistory(data); setError(''); setActive(null); setActiveTime(null) }
      } catch {
        if (!controller.signal.aborted) setError('History could not be loaded.')
      } finally { pending = false }
    }
    void reload()
    const timer = window.setInterval(() => { if (!document.hidden) void reload() }, 60000)
    const visible = () => { if (!document.hidden) void reload() }
    document.addEventListener('visibilitychange', visible)
    return () => { controller.abort(); window.clearInterval(timer); document.removeEventListener('visibilitychange', visible) }
  }, [deviceId, hours, canFetchHistory, expanded, fetchHistory, historyPath, metric])

  const points = history?.metrics[metric] ?? []
  const settings = history?.settings ?? []
  const height = settings.length ? 300 : 220
  const top = settings.length ? 82 : 44
  const bottom = settings.length ? 52 : 32
  const right = settings.length ? 40 : 12
  const legend = [...new Map(settings.map((setting) => [settingClass(setting), setting])).values()]
  const qualityByTime = new Map(history?.metrics.air_quality?.map((point) => [point.recorded_at, point.value]))
  const level = (point: Point) => qualityByTime.get(point.recorded_at) ?? 0
  const qualityClass = (point: Point) => qualityClasses[level(point)] ?? qualityClasses[0]
  const max = Math.max(5, Math.ceil(Math.max(...points.map((point) => point.max), 0) / 5) * 5)
  const min = 0
  const x = (time: number) => left + (time - (history?.start ?? 0)) / ((history?.end ?? 1) - (history?.start ?? 0)) * (width - left - right)
  const y = (value: number) => top + (max - value) / (max - min) * (height - top - bottom)
  const maxFan = Math.max(1, ...settings.map((setting) => settingFan(setting) ?? 0))
  const fanY = (value: number) => top + (maxFan - value) / maxFan * (height - top - bottom)
  const fanSegments: Setting[][] = []
  for (const setting of settings) {
    if (settingFan(setting) === null) continue
    const segment = fanSegments[fanSegments.length - 1]
    if (segment && segment[segment.length - 1].end_at === setting.recorded_at) segment.push(setting)
    else fanSegments.push([setting])
  }
  const segments: Point[][] = []
  for (const point of points) {
    const segment = segments[segments.length - 1]
    if (!segment || point.recorded_at - segment[segment.length - 1].recorded_at > (history?.interval_seconds ?? 60) * 1.5) segments.push([point])
    else segment.push(point)
  }
  const selected = active === null ? null : points[active]
  const selectedSetting = activeTime === null ? null : [...settings].reverse().find((setting) =>
    activeTime >= setting.recorded_at && activeTime <= setting.end_at)
  const runningDiffers = selectedSetting?.power === 'on' && selectedSetting.mode === 'manual'
    && selectedSetting.reported_fan_level !== null && selectedSetting.reported_fan_level !== selectedSetting.fan_level
  const ticks = [0, max / 2, max]
  const dateLabel = (time: number) => new Date(time * 1000).toLocaleString(undefined, hours === 24
    ? { hour: '2-digit', minute: '2-digit' } : { month: 'short', day: 'numeric' })

  function inspectPoint(event: React.PointerEvent<SVGSVGElement>) {
    if (!history) return
    const bounds = event.currentTarget.getBoundingClientRect()
    const pointerX = (event.clientX - bounds.left) / bounds.width * width
    setActiveTime(history.start + Math.max(0, Math.min(1, (pointerX - left) / (width - left - right))) * (history.end - history.start))
    if (!points.length) return
    let nearest = 0
    for (let index = 1; index < points.length; index++) {
      if (Math.abs(x(points[index].recorded_at) - pointerX) < Math.abs(x(points[nearest].recorded_at) - pointerX)) nearest = index
    }
    setActive(nearest)
  }

  return <section className="air-history" aria-labelledby={titleId}>
    <div className="history-header">
      <span id={titleId} data-testid="history-title">History</span>
      <button type="button" className="history-toggle" data-testid="history-toggle" aria-label={`${historyLabel} history for ${name}`} aria-expanded={expanded}
        aria-controls={contentId} onClick={() => { setExpanded(!expanded); setActive(null) }}><span aria-hidden="true" /></button>
    </div>
    <div id={contentId} ref={content} hidden={!expanded}>
    {expanded && <div className="history-controls" data-testid="history-controls">
      {metricSelector}
      <div className="history-periods" data-testid="history-periods" role="group" aria-label={`History period for ${name}`}>
        {[[24, '24h'], [168, '7d'], [720, '30d']].map(([period, label]) => <button key={period} type="button"
          data-testid={`history-period-${label}`} aria-pressed={hours === period} onClick={() => { setHours(Number(period)); setActive(null) }}>{label}</button>)}
      </div>
    </div>}
    {expanded && <>
      {!hasSensor && <p className="history-note" data-testid="history-unavailable">{metricLabel} readings unavailable.</p>}
      {canFetchHistory && <>
        {error && <p className="inline-error" role="status">{error}</p>}
        {!history && !error && <p className="history-empty" data-testid="history-loading">Loading history…</p>}
        {history && points.length === 0 && settings.length === 0 && <p className="history-empty">No readings recorded in this period.</p>}
        {history && (points.length > 0 || settings.length > 0) && <>
          {settings.length > 0 && <div className="history-settings-legend" data-testid="history-settings-legend">
            <span>Settings</span>
            {legend.map((setting) => <span key={settingClass(setting)} className={settingClass(setting)}>
              {setting.power === 'off' ? 'Off' : setting.mode ? setting.mode[0].toUpperCase() + setting.mode.slice(1) : 'Unknown'}
            </span>)}
            <span className="history-fan-legend">Fan speed</span>
          </div>}
          <svg className="history-chart" data-testid="history-chart" viewBox={`0 0 ${width} ${height}`} role="img"
            aria-label={`${historyLabel} history for ${name}. ${points.length} recorded intervals.${settings.length ? ' Purifier settings overlaid.' : ''}`}
            onPointerEnter={inspectPoint} onPointerMove={inspectPoint} onPointerDown={inspectPoint}
            onPointerLeave={(event) => {
              if (event.pointerType !== 'touch') { setActive(null); setActiveTime(null) }
            }}>
            <defs>{segments.map((segment, index) => <linearGradient key={index} id={`${gradientId}-${index}`} gradientUnits="userSpaceOnUse"
              x1={x(segment[0].recorded_at)} x2={x(segment[segment.length - 1].recorded_at)} y1={0} y2={0}>
              {segment.map((point, pointIndex) => <stop key={pointIndex} offset={segment.length === 1 ? 0 :
                (point.recorded_at - segment[0].recorded_at) / (segment[segment.length - 1].recorded_at - segment[0].recorded_at)} className={`history-gradient-stop ${qualityClass(point)}`} />)}
            </linearGradient>)}</defs>
            {settings.map((setting, index) => <g key={index} className={`history-setting ${settingClass(setting)}`}
              data-testid="history-setting" aria-label={settingLabel(setting)}
              aria-description={new Date(setting.recorded_at * 1000).toLocaleString()}>
              <rect className="history-setting-band" x={x(setting.recorded_at)} y={top}
                width={Math.max(1, x(setting.end_at) - x(setting.recorded_at))} height={height - top - bottom} />
              <rect className="history-setting-strip" x={x(setting.recorded_at)} y={height - bottom + 6}
                width={Math.max(1, x(setting.end_at) - x(setting.recorded_at))} height={12} />
            </g>)}
            {ticks.map((tick) => <g key={tick}>
              <line className="history-grid" x1={left} x2={width - right} y1={y(tick)} y2={y(tick)} />
              <text className="history-value-label" x={left - 6} y={y(tick) + 3}>{tick}</text>
            </g>)}
            {segments.map((segment, index) => <g key={index} data-testid="history-segment">
              <path className="history-range" fill={`url(#${gradientId}-${index})`} d={`M${segment.map((point) => `${x(point.recorded_at)},${y(point.max)}`).join(' L')} L${[...segment].reverse().map((point) => `${x(point.recorded_at)},${y(point.min)}`).join(' L')} Z`} />
              <path className="history-line" stroke={`url(#${gradientId}-${index})`} d={`M${segment.map((point) => `${x(point.recorded_at)},${y(point.value)}`).join(' L')}`} />
              {segment.length === 1 && <circle className={`history-dot ${qualityClass(segment[0])}`} cx={x(segment[0].recorded_at)} cy={y(segment[0].value)} />}
            </g>)}
            {settings.length > 0 && <g className="history-fan-speed" aria-label="Fan speed">
              <text className="history-fan-label" x={width - right + 8} y={top - 10}>Fan</text>
              {[...new Set([0, Math.ceil(maxFan / 2), maxFan])].map((tick) => <text key={tick}
                className="history-fan-label" x={width - right + 8} y={fanY(tick) + 3}>{tick}</text>)}
              {fanSegments.map((segment, index) => <g key={index}>
                <path className="history-fan-line" d={`M${segment.map((setting) =>
                  `${x(setting.recorded_at)},${fanY(settingFan(setting)!)} H${x(setting.end_at)}`).join(' L')}`} />
                {segment.length === 1 && segment[0].recorded_at === segment[0].end_at && <circle className="history-fan-dot"
                  cx={x(segment[0].recorded_at)} cy={fanY(settingFan(segment[0])!)} r={2} />}
              </g>)}
            </g>}
            {[0, 0.5, 1].map((fraction) => <text key={fraction} x={left + fraction * (width - left - right)} y={height - 8}
              className={`history-time-label ${fraction === 0 ? 'first' : fraction === 1 ? 'last' : ''}`}>{dateLabel(history.start + fraction * (history.end - history.start))}</text>)}
            {active !== null && selected && <g className={`history-selection ${qualityClass(selected)}`} data-testid="history-selection">
              <line className="history-cursor" x1={x(activeTime ?? selected.recorded_at)} x2={x(activeTime ?? selected.recorded_at)} y1={top} y2={height - bottom} />
              <circle className="history-dot" cx={x(selected.recorded_at)} cy={y(selected.value)} />
              <text className="history-reading" data-testid="history-reading" role="status" x={left} y={22}>
                {new Date(selected.recorded_at * 1000).toLocaleString(undefined, { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' })}
                {' · '}{valueLabel(selected.value, unit)}
              </text>
            </g>}
            {activeTime !== null && settings.length > 0 && <text className="history-reading" data-testid="history-setting-reading"
              role="status" x={left} y={selected ? 44 : 22}>
              {dateLabel(activeTime)}{' · '}{selectedSetting ? settingLabel(selectedSetting) : 'Setting unavailable'}
              {runningDiffers && <tspan x={left} dy={18}>Running fan {selectedSetting.reported_fan_level}</tspan>}
            </text>}
          </svg>
        </>}
      </>}
    </>}
    </div>
  </section>
}
