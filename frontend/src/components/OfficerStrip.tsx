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
interface NewCount { new_not_in_master?: number }

// Officer briefing strip: last-24h live-evidence money movement + rail triage
// counts. Amounts are linked-row sums (operator chips may include bonus — see
// Ledger tab for ex-bonus cash). Rails stay candidate/analyst-conclusion.
export function OfficerStrip() {
  const [dep, setDep] = useState<Flows | null>(null)
  const [wd, setWd] = useState<Flows | null>(null)
  const [high, setHigh] = useState<number | null>(null)
  const [newRails, setNewRails] = useState<number | null>(null)
  const [newVpa, setNewVpa] = useState<number | null>(null)
  const [newWal, setNewWal] = useState<number | null>(null)
  const [alerts, setAlerts] = useState<RailItem[]>([])
  const [alertsOff, setAlertsOff] = useState(false)

  const refresh = useCallback(() => {
    const cutoff = new Date(Date.now() - 864e5).toISOString().slice(0, 19) + 'Z'
    api<Flows>('/api/money-flows', { direction: 'deposit', source: 'object', since: cutoff, limit: 1 }).then(setDep).catch(() => {})
    api<Flows>('/api/money-flows', { direction: 'withdrawal', source: 'object', since: cutoff, limit: 1 }).then(setWd).catch(() => {})
    api<Rails>('/api/mule-rails', { priority: 'high', limit: 1 }).then((r) => setHigh(r.total ?? 0)).catch(() => {})
    api<Rails>('/api/mule-rails', { limit: 1, new_since: cutoff }).then((r) => setNewRails(r.total ?? 0)).catch(() => {})
    api<RailsList>('/api/mule-rails', { priority: 'high', limit: 5, new_since: cutoff }).then((r) => setAlerts(r.items ?? [])).catch(() => {})
    api<NewCount>('/api/upis', { limit: 1 }).then((r) => setNewVpa(r.new_not_in_master ?? 0)).catch(() => {})
    api<NewCount>('/api/wallets', { limit: 1 }).then((r) => setNewWal(r.new_not_in_master ?? 0)).catch(() => {})
  }, [])
  useEffect(refresh, [refresh])
  useEffect(() => {
    const t = setInterval(() => { if (!tabHidden()) refresh() }, 60000)
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
      <div className="st-card st-card-warn"><div className="st-card-label">High-priority rails</div><div className="st-card-value warn">{high ?? '—'}</div><div className="tiny dim">{newRails ?? '—'} first seen in 24h · candidates, not attribution</div></div>
      <div className="st-card"><div className="st-card-label">NEW VPAs / wallets</div><div className="st-card-value">{newVpa ?? '—'} / {newWal ?? '—'}</div><div className="tiny dim">not in master lists</div></div>
    </section>
    </>
  )
}
