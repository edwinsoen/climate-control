import type { AutomationSummary } from './automation'
import type { ControlOptions, DeviceDisplay } from './DeviceControls'

export type Api = <T>(path: string, options?: RequestInit) => Promise<T>

export type Device = {
  id: string; account_id: string; account_label: string; name: string; model: string
  supported: boolean; capabilities: string[]; state: Record<string, string | number | null>
  display?: DeviceDisplay
  retrieved_at: string | null; stale: boolean; error: string | null
  automation?: AutomationSummary | null
  controls: ControlOptions; control_available: boolean; control_retry_at: string | null
}

export type ControlResult = { retryAt: string | null; confirmed: boolean }

export function newerDevice(current: Device | undefined, incoming: Device): Device {
  return current && (Date.parse(current.retrieved_at ?? '') || 0) > (Date.parse(incoming.retrieved_at ?? '') || 0) ? current : incoming
}

export function isOffline(device: Device) {
  return device.state.connectivity === 'offline'
}

export function reading(device: Device, key: string, suffix = '') {
  if (!device.capabilities.includes(key)) return 'Not supported'
  const value = device.state[key]
  return value === null || value === undefined || value === 'unknown' ? 'Unknown' : `${value}${suffix}`
}

export function numeric(device: Device, key: string): number | null {
  const value = device.state[key]
  return device.capabilities.includes(key) && typeof value === 'number' ? value : null
}

/** Current setting in words, e.g. "Level 3", "Sleep", "Built-in auto", "Off". */
export function settingText(state: Device['state']) {
  if (state.power === 'off') return 'Off'
  if (state.mode === 'manual' && state.fan_level !== null && state.fan_level !== undefined) return `Level ${state.fan_level}`
  if (state.mode === 'sleep') return 'Sleep'
  if (state.mode === 'auto') return 'Built-in auto'
  return state.power === 'on' ? 'On' : 'Unknown'
}

export function deviceControls(api: Api, device: Device, onChange: (device: Device) => void) {
  const send = async (path: string, method: string, body: unknown): Promise<ControlResult> => {
    const updated = await api<Device & { control_confirmed: boolean }>(`/api/devices/${device.id}/${path}`, { method, body: JSON.stringify(body) })
    onChange(updated)
    return { retryAt: updated.control_retry_at, confirmed: updated.control_confirmed }
  }
  return {
    onControl: (command: unknown) => send('control', 'POST', command),
    onDisplayChange: (change: unknown) => send('display-settings', 'PUT', change),
  }
}
