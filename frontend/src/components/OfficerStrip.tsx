import { useCallback, useEffect, useState } from 'react'
import { api, tabHidden } from '../lib/api'

interface Rails { total: number }
interface RailItem {
  rail_type: string; rail_id: string; account_holders: string;
  deposit_transactions: number; gamblers: number;
  candidate_score: number; priority: string; first_seen: string;
}
interface RailsList extends Rails { items: RailItem[] }

export function OfficerStrip() {
  const [alerts, setAlerts] = useState<RailItem[]>([])
  const [alertsOff, setAlertsOff] = useState(false)

  const refresh = useCallback(() => {
    const cutoff = new Date(Date.now() - 864e5).toISOString().slice(0, 19) + 'Z'
    api<RailsList>('/api/mule-rails', { priority: 'high', limit: 5, new_since: cutoff }).then((r) => setAlerts(r.items ?? [])).catch(() => {})
  }, [])
  useEffect(refresh, [refresh])
  useEffect(() => {
    // 20s refresh (was 60s — late-sync fix 2026-09-16): the strip now makes
    // only 3 light summary calls, so a tighter poll keeps Overview in step
    // with the 10s backend collector without hammering the API.
    const t = setInterval(() => { if (!tabHidden()) refresh() }, 20000)
    return () => clearInterval(t)
  }, [refresh])

  return (
    <>
    {alerts.length > 0 && !alertsOff && (
      <div className="money-error" style={{ marginBottom: 12 }} title="Candidate rails — analyst conclusion, not attribution">
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
          <b>⚠ {alerts.length} NEW high-priority rail{alerts.length > 1 ? 's' : ''} in 24h — corroborate in Mule rails</b>
          <button className="btn" onClick={() => setAlertsOff(true)}>Dismiss</button>
        </div>
        {alerts.map((r) => (
          <div key={`${r.rail_type}:${r.rail_id}`} className="tiny" style={{ marginTop: 4 }}>
            <span className={`mule-priority priority-${r.priority}`}>{r.priority} · {r.candidate_score}</span>{' '}
            <span className="mono">{r.rail_id}</span>{' '}
            <span className="dim">{r.rail_type} · {r.gamblers} gamblers · {r.deposit_transactions} deposits · first seen {r.first_seen || '—'}{r.account_holders ? ` · ${r.account_holders}` : ''}</span>
          </div>
        ))}
      </div>
    )}
    </>
  )
}
