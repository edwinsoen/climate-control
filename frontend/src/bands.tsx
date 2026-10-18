export type Band = 'good' | 'moderate' | 'unhealthy' | 'very-unhealthy'

const LABELS: Record<Band, string> = {
  good: 'Good', moderate: 'Moderate', unhealthy: 'Unhealthy', 'very-unhealthy': 'Very unhealthy',
}

// PM2.5 breakpoints in µg/m³ (US EPA 24-hour) and AQI index breakpoints.
export const PM25_BREAKS = [12, 35, 55]
export const AQI_BREAKS = [50, 100, 150]

function bandFor(value: number, breaks: number[]): Band {
  if (value <= breaks[0]) return 'good'
  if (value <= breaks[1]) return 'moderate'
  if (value <= breaks[2]) return 'unhealthy'
  return 'very-unhealthy'
}

export function pm25Band(value: number) { return bandFor(value, PM25_BREAKS) }
export function aqiBand(value: number) { return bandFor(value, AQI_BREAKS) }

export function BandBadge({ band, testId }: { band: Band | 'offline'; testId?: string }) {
  return <span className={`band band-${band}`} data-testid={testId}>{band === 'offline' ? 'Offline' : LABELS[band]}</span>
}
