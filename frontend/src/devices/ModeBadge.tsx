import { Icon } from '../components/Icon'
import type { Mode } from './automation'

export function ModeBadge({ mode, paused }: { mode: Mode; paused?: boolean }) {
  if (paused) return <span className="badge" data-testid="device-mode-badge"><Icon name="pause" />Overridden</span>
  if (mode === 'auto') return <span className="badge auto" data-testid="device-mode-badge"><Icon name="auto" />Auto</span>
  if (mode === 'custom') return <span className="badge" data-testid="device-mode-badge"><Icon name="list" />Custom rules</span>
  return <span className="badge outline" data-testid="device-mode-badge">Manual</span>
}
