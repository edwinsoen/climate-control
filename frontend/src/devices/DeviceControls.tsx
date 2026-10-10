import { useEffect, useState } from 'react'

export type ControlOptions = { power?: boolean; modes?: string[]; fan_levels?: number[] }
type Command = { power: boolean } | { mode: string } | { fan_level: number }
const OPTIMISTIC_CONFIRMATION_MS = 90_000
type ControlledDevice = {
  name: string; controls: ControlOptions; control_available: boolean; control_retry_at: string | null
  state: Record<string, string | number | null>
}

function matches(command: Command, state: ControlledDevice['state']) {
  if ('power' in command) return state.power === (command.power ? 'on' : 'off')
  if ('mode' in command) return state.mode === command.mode
  return state.mode === 'manual' && state.fan_level === command.fan_level
}

export function DeviceControls({ device, onControl }: {
  device: ControlledDevice; onControl: (command: Command) => Promise<{ retryAt: string | null; confirmed: boolean }>
}) {
  const [pending, setPending] = useState(false)
  const [optimistic, setOptimistic] = useState<Command | null>(null)
  const [error, setError] = useState('')
  const [retryAt, setRetryAt] = useState(0)
  const [coolingDown, setCoolingDown] = useState(false)
  const deadline = retryAt
  useEffect(() => {
    const remaining = deadline - Date.now()
    setCoolingDown(remaining > 0)
    if (remaining <= 0) return
    const timer = window.setTimeout(() => setCoolingDown(false), remaining)
    return () => window.clearTimeout(timer)
  }, [deadline])

  useEffect(() => {
    if (!pending && optimistic && matches(optimistic, device.state)) {
      setOptimistic(null); setError('')
    }
  }, [device.state, optimistic, pending])

  useEffect(() => {
    if (pending || !optimistic) return
    // New readings do not restart this window. On expiry, show the latest
    // observed setting even if it contradicts the requested change.
    const timer = window.setTimeout(() => setOptimistic(null), OPTIMISTIC_CONFIRMATION_MS)
    return () => window.clearTimeout(timer)
  }, [optimistic, pending])

  const options = device.controls ?? {}
  const modes = ['auto', 'sleep'].filter((mode) => options.modes?.includes(mode))
  const levels = options.fan_levels ?? []
  const state = { ...device.state }
  if (optimistic && 'power' in optimistic) state.power = optimistic.power ? 'on' : 'off'
  if (optimistic && 'mode' in optimistic) state.mode = optimistic.mode
  if (optimistic && 'fan_level' in optimistic) {
    state.mode = 'manual'; state.fan_level = optimistic.fan_level
  }
  const on = state.power === 'on'
  const disabled = pending || coolingDown || !device.control_available || device.state.connectivity === 'offline'
  const selected = state.mode === 'manual' && levels.includes(Number(state.fan_level))
    ? `fan-${state.fan_level}` : modes.includes(String(state.mode)) ? String(state.mode) : ''

  async function control(command: Command) {
    if (disabled) return
    setPending(true); setOptimistic(command); setError('')
    try {
      const next = await onControl(command)
      setRetryAt(next.retryAt ? new Date(next.retryAt).getTime() : Date.now() + 5000)
      if (next.confirmed) setOptimistic(null)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'The change could not be completed.')
      setRetryAt(Date.now() + 5000)
      setOptimistic(null)
    } finally { setPending(false) }
  }

  return <div className="device-controls" data-testid="device-controls" aria-busy={pending}>
    {options.power && <div className="device-control-row"><span>Power</span>
      <button type="button" className="power-control" data-testid="device-power" role="switch" aria-label={`Power for ${device.name}`} aria-checked={on}
        disabled={disabled} onClick={() => void control({ power: !on })}>
        <span className="power-track" aria-hidden="true"><span /></span>{on ? 'On' : 'Off'}
      </button>
    </div>}
    {(modes.length > 0 || levels.length > 0) && <label className="device-control-row"><span>Fan</span>
      <select data-testid="device-fan" aria-label={`Fan for ${device.name}`} value={selected} disabled={disabled || !on}
        onChange={(event) => void control(event.target.value.startsWith('fan-')
          ? { fan_level: Number(event.target.value.slice(4)) } : { mode: event.target.value })}>
        {!selected && <option value="" disabled>Unknown</option>}
        {modes.map((mode) => <option data-testid="device-fan-option" key={mode} value={mode}>{mode === 'auto' ? 'Auto' : 'Sleep'}</option>)}
        {levels.map((level) => <option data-testid="device-fan-option" key={level} value={`fan-${level}`}>Level {level}</option>)}
      </select>
    </label>}
    {error && <p data-testid="device-control-error" className="inline-error" role="alert">{error}</p>}
  </div>
}
