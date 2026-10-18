import { useState } from 'react'
import type { Outdoor } from './OutdoorCard'
import { Icon } from '../components/Icon'

type Station = { location_id: number; name: string; aqi: number; updated_at?: string; measured_at?: string }

function locationError(error: unknown) {
  switch ((error as { code?: number } | null)?.code) {
    case 1: return 'Location access is blocked. Check browser and device location permissions, or search for a city.'
    case 2: return 'Could not determine your location. Check device location services, or search for a city.'
    case 3: return 'Location lookup timed out. Try again or search for a city.'
    default: return 'Location lookup failed. Try again or search for a city.'
  }
}

export function LocationForm({ api, onSave, onCancel }: {
  api: <T>(path: string, options?: RequestInit) => Promise<T>
  onSave: (outdoor: Outdoor) => void; onCancel?: () => void
}) {
  const [query, setQuery] = useState('')
  const [stations, setStations] = useState<Station[] | null>(null)
  const [selected, setSelected] = useState<number | null>(null)
  const [activity, setActivity] = useState('')
  const busy = Boolean(activity)
  const [error, setError] = useState('')

  function edit(value: string) {
    setQuery(value); setStations(null); setSelected(null); setError('')
  }

  async function findStations(path: string, body: object) {
    setActivity('Finding stations…'); setError(''); setStations(null); setSelected(null)
    try {
      const results = await api<{ stations: Station[] }>(path, {
        method: 'POST', body: JSON.stringify(body),
      })
      setStations(results.stations)
    } catch (err) { setError((err as Error).message) }
    finally { setActivity('') }
  }

  async function locate() {
    if (!window.isSecureContext) { setError('Location access requires HTTPS or localhost. Search for a city.'); return }
    if (!navigator.geolocation) { setError('This browser does not support location access. Search for a city.'); return }
    setActivity('Getting your location…'); setError(''); setStations(null); setSelected(null)
    let location: GeolocationPosition
    try {
      location = await new Promise<GeolocationPosition>((resolve, reject) =>
        navigator.geolocation.getCurrentPosition(resolve, reject, {
          timeout: 30_000, maximumAge: 300_000, enableHighAccuracy: true,
        }))
    } catch (err) { setError(locationError(err)); setActivity(''); return }
    await findStations('/api/outdoor/stations/nearby', {
      latitude: location.coords.latitude, longitude: location.coords.longitude,
    })
  }

  async function search(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault()
    await findStations('/api/outdoor/stations/search', { query: query.trim() })
  }

  async function save() {
    if (selected === null) return
    setActivity('Saving location…'); setError('')
    try {
      onSave(await api<Outdoor>('/api/outdoor/location', { method: 'PUT', body: JSON.stringify({ location_id: selected }) }))
    } catch (err) { setError((err as Error).message) }
    finally { setActivity('') }
  }

  return <form className="outdoor-location-form" data-testid="outdoor-location-form" onSubmit={search}>
    <div className="search-field">
      <label>City or station
        <input type="search" required autoFocus minLength={2} maxLength={120} value={query} placeholder="e.g. Clementi"
          data-testid="outdoor-city-query" onChange={(event) => edit(event.target.value)} disabled={busy} />
      </label>
      <button className="button" data-testid="outdoor-search-stations" disabled={busy || query.trim().length < 2}>Search</button>
    </div>
    <div className="actions">
      <button type="button" className="button small" data-testid="outdoor-use-location" onClick={() => void locate()} disabled={busy}><Icon name="pin" />Use my location</button>
      {onCancel && <button type="button" className="button small ghost" data-testid="outdoor-cancel-location" onClick={onCancel} disabled={busy}>Cancel</button>}
    </div>
    {busy && <p className="outdoor-loading reading-time" role="status" data-testid="outdoor-location-loading">
      <span className="outdoor-spinner" aria-hidden="true" />{activity}
    </p>}
    {error && <p className="inline-error" role="alert" data-testid="outdoor-location-error">{error}</p>}
    {stations && stations.length === 0 && <p className="subtle" data-testid="outdoor-no-stations">No reporting stations found. Try another city or station name.</p>}
    {stations && stations.length > 0 && <>
      <fieldset className="outdoor-stations"><legend>AQICN stations</legend>
        {stations.map((station) => <label key={station.location_id} className="outdoor-station-option" data-testid="outdoor-station-option">
          <input type="radio" name="outdoor-station" data-testid={`outdoor-station-${station.location_id}`} value={station.location_id} checked={selected === station.location_id}
            onChange={() => setSelected(station.location_id)} disabled={busy} />
          <span>{station.name}<small className="hint">AQI {station.aqi}{station.updated_at && ` · Updated ${station.updated_at}`}</small></span>
        </label>)}
      </fieldset>
      <button type="button" className="button primary small" data-testid="outdoor-save-location" disabled={busy || selected === null} onClick={() => void save()}>Save location</button>
    </>}
    {!stations && <p className="hint">AQICN · history starts when selected</p>}
  </form>
}
