import { useEffect, useRef, useState } from 'react'
import { automationPath, eventDetail, eventText, isActivity, type AutomationEvent } from '../devices/automation'
import type { Api, Device } from '../devices/types'
import { clock } from '../format'

type Item = AutomationEvent & { device: Device }

export function ActivityFeed({ api, devices }: { api: Api; devices: Device[] }) {
  const [items, setItems] = useState<Item[] | null>(null)
  const current = useRef(devices)
  current.current = devices
  const ids = devices.filter((device) => device.supported).map((device) => device.id).join(',')

  useEffect(() => {
    let cancelled = false
    const load = async () => {
      const midnight = new Date().setHours(0, 0, 0, 0) / 1000
      const lists = await Promise.all(current.current.filter((device) => device.supported).map((device) =>
        api<AutomationEvent[]>(automationPath(device.id) + '/events')
          .then((events) => events.filter((event) => event.created_at >= midnight && isActivity(event)).map((event) => ({ ...event, device })))
          .catch(() => [] as Item[])))
      if (!cancelled) setItems(lists.flat().sort((a, b) => b.created_at - a.created_at).slice(0, 8))
    }
    void load()
    const timer = window.setInterval(() => { if (!document.hidden) void load() }, 60_000)
    return () => { cancelled = true; window.clearInterval(timer) }
    // Reload when the set of devices changes, not on every status poll.
  }, [api, ids])

  return <section className="card activity-card" data-testid="activity-feed" aria-labelledby="activity-title">
    <h2 id="activity-title">What Auto did today</h2>
    {items && items.length === 0 && <p className="subtle">No activity yet.</p>}
    {items && items.length > 0 && <ol className="activity-list">{items.map((item) => <li key={`${item.device.id}-${item.id}`} data-testid="activity-item">
      <time dateTime={new Date(item.created_at * 1000).toISOString()}>{clock(item.created_at)}</time>
      <div><strong>{item.device.name} · {eventText(item)}</strong>{eventDetail(item) && <span>{eventDetail(item)}</span>}</div>
    </li>)}</ol>}
  </section>
}
