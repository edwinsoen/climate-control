import { useState } from 'react'
import { durationText, PAUSE_OPTIONS, putConfiguration, type AutomationState } from './automation'
import type { Api } from './types'

/** How long a manual control change pauses automation for this device. */
export function OverrideDuration({ api, deviceId, state, onChange, label = 'Override for', disabled }: {
  api: Api; deviceId: string; state: AutomationState; onChange: (state: AutomationState) => void
  label?: string; disabled?: boolean
}) {
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')

  async function update(seconds: number) {
    setBusy(true); setError('')
    try {
      onChange(await putConfiguration(api, deviceId, state, { pause_seconds: seconds }))
    } catch (err) { setError((err as Error).message) } finally { setBusy(false) }
  }

  return <>
    <label className="inline-field">{label}
      <select data-testid="automation-pause-duration" value={state.pause_seconds} disabled={busy || disabled}
        onChange={(event) => void update(Number(event.target.value))}>
        {!PAUSE_OPTIONS.includes(state.pause_seconds) && <option value={state.pause_seconds}>{durationText(state.pause_seconds)}</option>}
        {PAUSE_OPTIONS.map((seconds) => <option key={seconds} value={seconds}>{durationText(seconds)}</option>)}
      </select>
    </label>
    {error && <p className="inline-error" role="alert">{error}</p>}
  </>
}
