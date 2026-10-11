import { useState } from 'react'
import { AirQualityChart } from '../devices/AirQualityChart'
import { LocationForm } from './LocationForm'
import { OutdoorActions } from './OutdoorActions'
import { CardHeader } from '../components/CardHeader'
import { AirReading } from '../components/AirReading'

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

function age(value: string) {
  const minutes = Math.max(0, Math.floor((Date.now() - Date.parse(value)) / 60000))
  if (minutes < 1) return 'Just now'
  if (minutes < 60) return `${minutes} min ago`
  if (minutes < 1440) return `${Math.floor(minutes / 60)} hr ago`
  return `${Math.floor(minutes / 1440)} days ago`
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
  const selected = outdoor.capabilities.includes(metric) ? metric
    : 'aqi'
  return <section aria-labelledby="outdoor-title" data-testid="outdoor-section">
    <div className="section-heading"><h2 id="outdoor-title">Outdoor air</h2>
      {outdoor.available && !outdoor.configured && !editing && <button className="button small" data-testid="outdoor-set-location" onClick={() => { setEditing(true); setError('') }}>
        Set location
      </button>}
    </div>
    {error && <p className="inline-error" role="alert">{error}</p>}
    {editing && <LocationForm api={api} onCancel={() => setEditing(false)}
      onSave={(outdoor) => { setEditing(false); onChange(outdoor) }} />}
    {!outdoor.configured && !editing && <p className="subtle" data-testid="outdoor-no-location">
      {outdoor.available ? 'Set your location to see outdoor air quality.' : 'Outdoor data is not connected.'}
    </p>}
    {outdoor.configured && !editing && <article className="device-card outdoor-card" data-testid="outdoor-card" aria-busy={outdoor.loading}>
      <CardHeader name={outdoor.station?.name ?? 'Connecting station…'} testId="outdoor-station"
        actions={<OutdoorActions busy={busy} canChange={outdoor.available}
          onChangeLocation={() => { setEditing(true); setError('') }} onRemove={() => void remove()} />}
        description={<><a href="https://aqicn.org" target="_blank" rel="noreferrer">AQICN / WAQI</a>
        {outdoor.station?.url && <> · <a href={outdoor.station.url} target="_blank" rel="noreferrer">Station</a></>}
        {outdoor.station?.attribution?.map((source, index) => <span key={index}> · {source.url
          ? <a href={source.url} target="_blank" rel="noreferrer">{source.name}</a> : source.name}</span>)}
      </>} />
      <div className="air-readings">
        {(['aqi', 'pm25', 'pm10'] as const).map((key) => {
          const reading = outdoor.metrics[key]
          const preview = key === 'aqi' ? outdoor.preview : null
          return <AirReading key={key} label={key === 'aqi' ? 'AQI' : key === 'pm25' ? 'PM2.5 AQI' : 'PM10 AQI'}
            testId={`outdoor-${key}`} unit={preview || reading ? 'AQI' : undefined}
            value={preview ? preview.value.toLocaleString()
              : reading ? reading.value.toLocaleString()
              : outdoor.loading ? 'Loading…'
              : key === 'aqi' || outdoor.capabilities.includes(key) ? 'No reading yet' : 'Unavailable'}
            age={preview?.updated_at ? <span className="reading-time" data-testid="outdoor-aqi-preview-time">Reported {preview.updated_at}</span>
              : !preview && reading ? <span className={`reading-time ${reading.stale ? 'outdoor-stale' : ''}`}
              data-testid={`outdoor-${key}-age`} title={new Date(reading.measured_at).toLocaleString()}>
              {age(reading.measured_at)}{reading.stale && ' · Stale'}
            </span> : null} />
        })}
      </div>
      {outdoor.loading && <p className="outdoor-loading reading-time" role="status" data-testid="outdoor-loading">
        <span className="outdoor-spinner" aria-hidden="true" />Loading station readings…
      </p>}
      {outdoor.error && <p className="inline-error" role="status">{outdoor.error}</p>}
      <AirQualityChart deviceId={String(outdoor.location_id)} name={outdoor.station?.name ?? 'Outdoor air'}
        capabilities={outdoor.capabilities} fetchHistory={api} metric={selected} historyPath="/api/outdoor/history" unit="AQI"
        metricSelector={outdoor.capabilities.length > 1 && <select aria-label="Outdoor history metric" data-testid="outdoor-history-metric" value={selected}
          onChange={(event) => setMetric(event.target.value as 'aqi' | 'pm25' | 'pm10')}>
          <option value="aqi">AQI</option>
          {outdoor.capabilities.includes('pm25') && <option value="pm25">PM2.5 AQI</option>}
          {outdoor.capabilities.includes('pm10') && <option value="pm10">PM10 AQI</option>}
        </select>} />
    </article>}
  </section>
}
