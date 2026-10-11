import type { ReactNode } from 'react'

export function AirReading({ label, value, unit, valueTestId, testId, age }: {
  label: string; value: string; unit?: string; valueTestId?: string; testId?: string
  age?: ReactNode
}) {
  return <div className="main-reading">
    <p className="reading-label">{label}</p>
    <div className="reading-row">
      <div className="reading-value" data-testid={testId}>
        <strong data-testid={valueTestId}>{value}</strong>
        {unit && <>{' '}<span className="reading-unit">{unit}</span></>}
      </div>
      {age && <span className="reading-age">{' · '}{age}</span>}
    </div>
  </div>
}
