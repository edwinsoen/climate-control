import { useEffect, useId, useState } from 'react'
import { Icon } from '../components/Icon'

export type DisplayMode = 'manual' | 'warnings_only' | 'warnings_only_bright' | 'warnings_only_dim'
export type PhysicalDisplayState = 'on' | 'off' | 'bright' | 'dim'
export type DisplayOption = PhysicalDisplayState | 'warnings_only' | 'warnings_only_bright' | 'warnings_only_dim'
export type WarningThresholdId = 'who_2021_annual' | 'who_2021_24h' | 'who_2005_annual' | 'who_2005_24h'
export type DeviceDisplay = {
  state?: string | null
  mode: DisplayMode | null
  warning_threshold: WarningThresholdId | null
  effective_warning_threshold: WarningThresholdId
}
export type DisplayChange = { mode?: DisplayOption | DisplayMode; warning_threshold?: WarningThresholdId | null }
export type ControlOptions = {
  power?: boolean; modes?: string[]; fan_levels?: number[]
  display?: boolean; display_states?: PhysicalDisplayState[]; display_modes?: DisplayMode[]
}
type Command = { power: boolean } | { mode: string } | { fan_level: number }
const OPTIMISTIC_CONFIRMATION_MS = 90_000
const WARNING_MODES = new Set<string>(['warnings_only', 'warnings_only_bright', 'warnings_only_dim'])
type ControlledDevice = {
  name: string; controls: ControlOptions; control_available: boolean; control_retry_at: string | null
  state: Record<string, string | number | null>
  display?: DeviceDisplay
}

const DISPLAY_LABELS: Record<DisplayOption, string> = {
  on: 'On',
  bright: 'Bright',
  dim: 'Dim',
  off: 'Off',
  warnings_only: 'Warnings only',
  warnings_only_bright: 'Warnings only (bright)',
  warnings_only_dim: 'Warnings only (dim)',
}

export const WARNING_THRESHOLD_OPTIONS: { id: WarningThresholdId; label: string; name: string; pm25: number; pm10: number }[] = [
  { id: 'who_2021_24h', label: 'WHO 2021 24-h', name: 'WHO 2021 · 24-hour', pm25: 15, pm10: 45 },
  { id: 'who_2021_annual', label: 'WHO 2021 Annual', name: 'WHO 2021 · annual', pm25: 5, pm10: 15 },
  { id: 'who_2005_24h', label: 'WHO 2005 24-h', name: 'WHO 2005 · 24-hour', pm25: 25, pm10: 50 },
  { id: 'who_2005_annual', label: 'WHO 2005 Annual', name: 'WHO 2005 · annual', pm25: 10, pm10: 20 },
]

function matches(command: Command, state: ControlledDevice['state']) {
  if ('power' in command) return state.power === (command.power ? 'on' : 'off')
  if ('mode' in command) return state.mode === command.mode
  return state.mode === 'manual' && state.fan_level === command.fan_level
}

export function DeviceControls({ device, onControl, onDisplayChange }: {
  device: ControlledDevice
  onControl: (command: Command) => Promise<{ retryAt: string | null; confirmed: boolean }>
  onDisplayChange?: (change: DisplayChange) => Promise<{ retryAt: string | null; confirmed: boolean }>
}) {
  const [pending, setPending] = useState(false)
  const [optimistic, setOptimistic] = useState<Command | null>(null)
  const [optimisticDisplay, setOptimisticDisplay] = useState<DisplayOption | null>(null)
  const [error, setError] = useState('')
  const [retryAt, setRetryAt] = useState(0)
  const [coolingDown, setCoolingDown] = useState(false)
  const [sliderLevel, setSliderLevel] = useState<number | null>(null)
  const ids = useId()
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
    if (!pending && optimisticDisplay && (
      WARNING_MODES.has(optimisticDisplay)
        ? device.display?.mode === optimisticDisplay
        : !WARNING_MODES.has(device.display?.mode ?? '') && device.state.display === optimisticDisplay
    )) {
      setOptimisticDisplay(null); setError('')
    }
  }, [device.display?.mode, device.state, optimistic, optimisticDisplay, pending])

  useEffect(() => {
    if (pending || (!optimistic && !optimisticDisplay)) return
    // New readings do not restart this window. On expiry, show the latest
    // observed setting even if it contradicts the requested change.
    const timer = window.setTimeout(() => { setOptimistic(null); setOptimisticDisplay(null) }, OPTIMISTIC_CONFIRMATION_MS)
    return () => window.clearTimeout(timer)
  }, [optimistic, optimisticDisplay, pending])

  const options = device.controls ?? {}
  const modes = ['auto', 'sleep'].filter((mode) => options.modes?.includes(mode))
  const levels = options.fan_levels ?? []
  const physicalStates: PhysicalDisplayState[] = options.display_states ?? ['on', 'off']
  const warningModes = (options.display_modes ?? []).filter(
    (mode): mode is 'warnings_only' | 'warnings_only_bright' | 'warnings_only_dim' => WARNING_MODES.has(mode)
  )
  const displayOptions: DisplayOption[] = options.display
    ? [...physicalStates, ...warningModes]
    : []
  const state = { ...device.state }
  if (optimistic && 'power' in optimistic) state.power = optimistic.power ? 'on' : 'off'
  if (optimistic && 'mode' in optimistic) state.mode = optimistic.mode
  if (optimistic && 'fan_level' in optimistic) {
    state.mode = 'manual'; state.fan_level = optimistic.fan_level
  }
  const on = state.power === 'on'
  const disabled = pending || coolingDown || !device.control_available || device.state.connectivity === 'offline'
  const rawDisplay = optimisticDisplay ?? (
    device.display?.mode && WARNING_MODES.has(device.display.mode)
      ? device.display.mode
      : physicalStates.includes(state.display as PhysicalDisplayState) ? (state.display as PhysicalDisplayState) : ''
  )
  const selectedDisplay = displayOptions.includes(rawDisplay as DisplayOption) ? rawDisplay : ''
  const selectedThreshold = device.display?.warning_threshold ?? ''

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

  async function changeDisplay(change: DisplayChange) {
    if (disabled || !onDisplayChange) return
    const isWarningMode = Boolean(change.mode && WARNING_MODES.has(change.mode))
    setPending(true); if (change.mode && change.mode !== 'manual') setOptimisticDisplay(change.mode); setError('')
    try {
      const next = await onDisplayChange(change)
      const nextDeadline = next.retryAt ? new Date(next.retryAt).getTime() : 0
      if (nextDeadline > Date.now()) setRetryAt(nextDeadline)
      if (next.confirmed || isWarningMode || !change.mode) setOptimisticDisplay(null)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'The change could not be completed.')
      if (change.mode && !isWarningMode) setRetryAt(Date.now() + 5000)
      setOptimisticDisplay(null)
    } finally { setPending(false) }
  }

  const modeOptions = [
    ...(levels.length ? [['manual', 'Manual']] : []),
    ...(modes.includes('sleep') ? [['sleep', 'Sleep']] : []),
    ...(modes.includes('auto') ? [['auto', 'Built-in auto']] : []),
  ] as const
  const currentMode = String(state.mode ?? '')
  const currentLevel = Number(state.fan_level)
  const manualLevel = levels.includes(currentLevel) ? currentLevel : levels[0]
  const showSpeed = levels.length > 0 && (currentMode === 'manual' || modes.length === 0)
  const levelIndex = levels.indexOf(sliderLevel ?? currentLevel)
  const modeDisabled = disabled || !on

  function commitSlider() {
    if (sliderLevel !== null && sliderLevel !== currentLevel) void control({ fan_level: sliderLevel })
    setSliderLevel(null)
  }

  return <div className="device-controls" data-testid="device-controls" aria-busy={pending}>
    {options.power && <div className="control-row"><span>Power</span>
      <button type="button" className="switch" data-testid="device-power" role="switch" aria-label={`Power for ${device.name}`} aria-checked={on}
        disabled={disabled} onClick={() => void control({ power: !on })}><span aria-hidden="true" /></button>
    </div>}
    {modeOptions.length > 1 && <div className="control-group"><span id={`${ids}-mode`}>Mode</span>
      <div className="segmented fill" role="radiogroup" aria-labelledby={`${ids}-mode`}>
        {modeOptions.map(([mode, label]) => <button key={mode} type="button" role="radio" data-testid={`device-mode-${mode}`}
          aria-checked={currentMode === mode} disabled={modeDisabled}
          onClick={() => { if (currentMode !== mode) void control(mode === 'manual' ? { fan_level: manualLevel } : { mode }) }}>{label}</button>)}
      </div>
    </div>}
    {showSpeed && levels.length <= 5 && <div className="control-group"><span id={`${ids}-speed`}>Speed</span>
      <div className="speed-scale" role="radiogroup" aria-labelledby={`${ids}-speed`}>
        {levels.map((level) => <button key={level} type="button" role="radio" data-testid={`device-speed-${level}`}
          aria-checked={currentMode === 'manual' && currentLevel === level} aria-label={`Level ${level}`} disabled={modeDisabled}
          onClick={() => void control({ fan_level: level })}>{level}</button>)}
      </div>
    </div>}
    {showSpeed && levels.length > 5 && <div className="control-group">
      <div className="control-group-head"><label htmlFor={`${ids}-slider`}>Speed</label>
        <span data-testid="device-speed-value"><strong>{levelIndex >= 0 ? `Level ${levels[levelIndex]}` : 'Unknown'}</strong> of {levels[levels.length - 1]}{pending ? ' · sending' : ''}</span></div>
      <div className="stepper">
        <button type="button" className="icon-button" data-testid="device-speed-down" aria-label={`Lower speed for ${device.name}`}
          disabled={modeDisabled || levelIndex <= 0} onClick={() => void control({ fan_level: levels[levelIndex - 1] })}><Icon name="minus" /></button>
        <input id={`${ids}-slider`} type="range" data-testid="device-speed" min={0} max={levels.length - 1} step={1}
          value={Math.max(0, levelIndex)} disabled={modeDisabled} aria-valuetext={levelIndex >= 0 ? `Level ${levels[levelIndex]}` : 'Unknown'}
          onChange={(event) => setSliderLevel(levels[Number(event.target.value)])}
          onPointerUp={commitSlider} onKeyUp={commitSlider} onBlur={commitSlider} />
        <button type="button" className="icon-button" data-testid="device-speed-up" aria-label={`Raise speed for ${device.name}`}
          disabled={modeDisabled || levelIndex >= levels.length - 1} onClick={() => void control({ fan_level: levels[levelIndex < 0 ? 0 : levelIndex + 1] })}><Icon name="plus" /></button>
      </div>
      <div className="stepper-ends" aria-hidden="true"><span>{levels[0]}</span><span>{levels[levels.length - 1]}</span></div>
    </div>}
    {options.display && displayOptions.length > 0 && <label className="control-select">Display
      <select data-testid="device-display" aria-label={`Display for ${device.name}`} value={selectedDisplay} disabled={disabled || !on}
        onChange={(event) => void changeDisplay({ mode: event.target.value as DisplayOption })}>
        {!selectedDisplay && <option value="" disabled>Unknown</option>}
        {displayOptions.map((mode) => <option data-testid="device-display-option" key={mode} value={mode}>{DISPLAY_LABELS[mode]}</option>)}
      </select>
    </label>}
    {options.display && WARNING_MODES.has(selectedDisplay) && <label className="control-select">Warning target
      <select data-testid="device-display-threshold" aria-label={`Warning target for ${device.name}`} value={selectedThreshold} disabled={disabled}
        onChange={(event) => void changeDisplay({ warning_threshold: (event.target.value || null) as WarningThresholdId | null })}>
        <option value="">Global default</option>
        {WARNING_THRESHOLD_OPTIONS.map((option) => <option key={option.id} value={option.id}>{option.label}</option>)}
      </select>
    </label>}
    {error && <p data-testid="device-control-error" className="inline-error" role="alert">{error}</p>}
  </div>
}
