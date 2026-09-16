import { useCallback, useEffect, useState } from 'react'
import { api, tabHidden } from '../lib/api'
import { fmtINR, fmtTime } from '../lib/format'

interface Flows { transactions: number; sum_amount_inr: number; src_newest?: string | null }
interface Rails { total: number }
interface RailItem {
  rail_type: string; rail_id: string; account_holders: string;
  deposit_transactions: number; gamblers: number;
  candidate_score: number; priority: string; first_seen: string;
}
interface RailsList extends Rails { items: RailItem[] }

// Officer briefing strip: last-24h live-evidence money movement + rail triage
// counts. Amounts are linked-row sums (operator chips may include bonus — see
// Ledger tab for ex-bonus cash). Rails stay candidate/analyst-conclusion.
export function OfficerStrip() {
  const [dep, setDep] = useState<Flows | null>(null)
  const [wd, setWd] = useState<Flows | null>(null)
  const [alerts, setAlerts] = useState<RailItem[]>([])
  const [alertsOff, setAlertsOff] = useState(false)

  const refresh = useCallback(() => {
    const cutoff = new Date(Date.now() - 864e5).toISOString().slice(0, 19) + 'Z'
    api<Flows>('/api/money-flows', { direction: 'deposit', source: 'object', since: cutoff, limit: 1 }).then(setDep).catch(() => {})
    api<Flows>('/api/money-flows', { direction: 'withdrawal', source: 'object', since: cutoff, limit: 1 }).then(setWd).catch(() => {})
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
          <b>⚠ {alerts.length} NEW high-priority rail{alerts.length > 1 ? 's' : ''} in 24h — corroborate in Mule Money Flows</b>
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
    <section className="st-cards money-cards" style={{ marginBottom: 16 }} title="Officer briefing — last 24h live bucket evidence">
      <div className="st-card st-card-accent"><div className="st-card-label">Live deposits · 24h · history from Jul 22, 2026</div><div className="st-card-value accent">{(dep?.transactions ?? 0).toLocaleString()} txns</div><div className="tiny dim money">{fmtINR(dep?.sum_amount_inr)} · {(dep?.transactions ?? 0) === 0 ? `source quiet since ${fmtTime(dep?.src_newest)}` : `newest slip ${fmtTime(dep?.src_newest)}`}</div></div>
      <div className="st-card st-card-money"><div className="st-card-label">Live withdrawals · 24h</div><div className="st-card-value money">{(wd?.transactions ?? 0).toLocaleString()} txns</div><div className="tiny dim money">{fmtINR(wd?.sum_amount_inr)} · {(wd?.transactions ?? 0) === 0 ? `source quiet since ${fmtTime(wd?.src_newest)}` : `newest slip ${fmtTime(wd?.src_newest)}`}</div></div>
    </section>
    </>
  )
}
