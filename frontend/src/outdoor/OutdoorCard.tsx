import { useState } from 'react'
import { AirQualityChart } from '../devices/AirQualityChart'
import { LocationForm } from './LocationForm'
import { OutdoorActions } from './OutdoorActions'
import { aqiBand, BandBadge } from '../bands'
import { Icon } from '../components/Icon'

export type Outdoor = {
  available: boolean; configured: boolean; source: 'aqicn'; location_id: number | null
  coordinates: { latitude: number; longitude: number } | null
  station: {
    name: string; url: string | null
    attribution: { name: string; url: string | null }[]
  } | null
  capabilities: string[]
  preview: { value: number; updated_at: string | null } | null
  loading: boolean
  metrics: Partial<Record<'aqi' | 'pm25' | 'pm10', { value: number; unit: 'AQI'; measured_at: string; stale: boolean }>>
  last_success_at: string | null; error: string | null
  backfill: { status: 'unsupported'; error: string | null }
}

export function age(value: string) {
  const minutes = Math.max(0, Math.floor((Date.now() - Date.parse(value)) / 60000))
  if (minutes < 1) return 'Just now'
  if (minutes < 60) return `${minutes} min ago`
  if (minutes < 1440) return `${Math.floor(minutes / 60)} hr ago`
  return `${Math.floor(minutes / 1440)} days ago`
}

function Measurement({ outdoor, metric }: { outdoor: Outdoor; metric: 'aqi' | 'pm25' | 'pm10' }) {
  const reading = outdoor.metrics[metric]
  const preview = metric === 'aqi' ? outdoor.preview : null
  const value = preview ? preview.value.toLocaleString() : reading ? reading.value.toLocaleString()
    : outdoor.loading ? 'Loading…'
    : metric === 'aqi' || outdoor.capabilities.includes(metric) ? 'No reading yet' : 'Unavailable'
  return <>
    <span data-testid={`outdoor-${metric}`}>{value}{(preview || reading) && <> <span className="reading-unit">AQI</span></>}</span>
    {preview?.updated_at && <span className="reading-time" data-testid="outdoor-aqi-preview-time">Reported {preview.updated_at}</span>}
    {!preview && reading && <span className={`reading-time ${reading.stale ? 'outdoor-stale' : ''}`}
      data-testid={`outdoor-${metric}-age`} title={new Date(reading.measured_at).toLocaleString()}>
      {age(reading.measured_at)}{reading.stale && ' · Stale'}
    </span>}
  </>
}

export function OutdoorCard({ outdoor, api, onChange }: {
  outdoor: Outdoor; api: <T>(path: string, options?: RequestInit) => Promise<T>
  onChange: (outdoor: Outdoor) => void
}) {
  const [metric, setMetric] = useState<'aqi' | 'pm25' | 'pm10'>('aqi')
  const [editing, setEditing] = useState(false)
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)

  async function remove() {
    setBusy(true); setError('')
    try { onChange(await api<Outdoor>('/api/outdoor/location', { method: 'DELETE' })) }
    catch (err) { setError((err as Error).message) }
    finally { setBusy(false) }
  }
  const selected = outdoor.capabilities.includes(metric) ? metric : 'aqi'
  const aqi = outdoor.preview?.value ?? outdoor.metrics.aqi?.value
  const showCard = outdoor.configured && !editing
  return <section className="card" aria-labelledby="outdoor-title" data-testid="outdoor-section">
    {!showCard && <div className="card-header"><div><h2 id="outdoor-title">Outdoor air</h2></div></div>}
    {error && <p className="inline-error" role="alert">{error}</p>}
    {editing && <LocationForm api={api} onCancel={() => setEditing(false)}
      onSave={(outdoor) => { setEditing(false); onChange(outdoor) }} />}
    {!outdoor.configured && !editing && <>
      <p className="subtle" data-testid="outdoor-no-location">
        {outdoor.available ? 'Set your location to see outdoor air quality.' : 'Outdoor data is not connected.'}
      </p>
      {outdoor.available && <button className="button small" data-testid="outdoor-set-location" onClick={() => { setEditing(true); setError('') }}>
        <Icon name="pin" />Set location
      </button>}
    </>}
    {showCard && <div className="form-stack" data-testid="outdoor-card" aria-busy={outdoor.loading}>
      <div className="card-header">
        <div><h2 id="outdoor-title">Outdoor air</h2></div>
        <div className="actions">
          {outdoor.metrics.aqi && !outdoor.preview && <span className="reading-time">{age(outdoor.metrics.aqi.measured_at)}</span>}
          <OutdoorActions busy={busy} canChange={outdoor.available}
            onChangeLocation={() => { setEditing(true); setError('') }} onRemove={() => void remove()} />
        </div>
      </div>
      <div className="outdoor-headline">
        <strong><Measurement outdoor={outdoor} metric="aqi" /></strong>
        {aqi !== undefined && <BandBadge band={aqiBand(aqi)} />}
      </div>
      <dl className="outdoor-metrics">
        <div><dt>PM2.5 AQI</dt><dd><Measurement outdoor={outdoor} metric="pm25" /></dd></div>
        <div><dt>PM10 AQI</dt><dd><Measurement outdoor={outdoor} metric="pm10" /></dd></div>
      </dl>
      {outdoor.loading && <p className="outdoor-loading reading-time" role="status" data-testid="outdoor-loading">
        <span className="outdoor-spinner" aria-hidden="true" />Loading station readings…
      </p>}
      {outdoor.error && <p className="inline-error" role="status">{outdoor.error}</p>}
      <p className="outdoor-source">
        <span data-testid="outdoor-station">{outdoor.station?.name ?? 'Connecting station…'}</span>
        {' · '}<a href="https://aqicn.org" target="_blank" rel="noreferrer">AQICN / WAQI</a>
        {outdoor.station?.url && <> · <a href={outdoor.station.url} target="_blank" rel="noreferrer">Station</a></>}
        {outdoor.station?.attribution?.map((source, index) => <span key={index}> · {source.url
          ? <a href={source.url} target="_blank" rel="noreferrer">{source.name}</a> : source.name}</span>)}
      </p>
      <AirQualityChart deviceId={String(outdoor.location_id)} name={outdoor.station?.name ?? 'Outdoor air'}
        capabilities={outdoor.capabilities} fetchHistory={api} metric={selected} historyPath="/api/outdoor/history" unit="AQI"
        metricSelector={outdoor.capabilities.length > 1 && <select aria-label="Outdoor history metric" data-testid="outdoor-history-metric" value={selected}
          onChange={(event) => setMetric(event.target.value as 'aqi' | 'pm25' | 'pm10')}>
          <option value="aqi">AQI</option>
          {outdoor.capabilities.includes('pm25') && <option value="pm25">PM2.5 AQI</option>}
          {outdoor.capabilities.includes('pm10') && <option value="pm10">PM10 AQI</option>}
        </select>} />
    </div>}
  </section>
}
