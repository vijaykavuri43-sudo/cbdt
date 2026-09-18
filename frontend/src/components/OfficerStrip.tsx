import { useCallback, useEffect, useState } from 'react'
import { api, tabHidden } from '../lib/api'
import { fmtINR, fmtTime } from '../lib/format'

interface Flows { transactions: number; sum_amount_inr: number; src_newest?: string | null }
interface RailItem {
  rail_type: string
  rail_id: string
  account_holders: string
  deposit_transactions: number
  gamblers: number
  candidate_score: number
  priority: string
  first_seen: string
  last_seen?: string
  deposit_amount_inr?: number
}
interface RailsList {
  total: number
  items: RailItem[]
  summary?: { by_rail_type?: { upi?: number; crypto_wallet?: number; bank_account?: number } }
}

function railKind(t: string): string {
  if (t === 'upi') return 'UPI'
  if (t === 'crypto_wallet') return 'wallet'
  if (t === 'bank_account') return 'account'
  return t || 'rail'
}

function gotoMuleRails() {
  window.dispatchEvent(new CustomEvent('s3lm-goto', { detail: { tab: 'deposits' } }))
}

// Officer briefing: last-24h live money + high-priority rails that actually
// received a deposit in the last 24h (UPI / wallet / bank account).
export function OfficerStrip() {
  const [dep, setDep] = useState<Flows | null>(null)
  const [wd, setWd] = useState<Flows | null>(null)
  const [high24, setHigh24] = useState<number | null>(null)
  const [mix, setMix] = useState({ upi: 0, wallet: 0, account: 0 })
  const [alerts, setAlerts] = useState<RailItem[]>([])
  const [alertsOff, setAlertsOff] = useState(false)

  const refresh = useCallback(() => {
    const cutoff = new Date(Date.now() - 864e5).toISOString().slice(0, 19) + 'Z'
    api<Flows>('/api/money-flows', { direction: 'deposit', source: 'object', since: cutoff, limit: 1 }).then(setDep).catch(() => {})
    api<Flows>('/api/money-flows', { direction: 'withdrawal', source: 'object', since: cutoff, limit: 1 }).then(setWd).catch(() => {})
    const highQ = { priority: 'high', source: 'object', active_since: cutoff }
    Promise.all([
      api<RailsList>('/api/mule-rails', { ...highQ, limit: 8 }),
      api<RailsList>('/api/mule-rails', { ...highQ, limit: 2, rail_type: 'crypto_wallet' }),
      api<RailsList>('/api/mule-rails', { ...highQ, limit: 2, rail_type: 'bank_account' }),
    ]).then(([all, wallets, accounts]) => {
      setHigh24(all.total ?? 0)
      const t = all.summary?.by_rail_type || {}
      setMix({
        upi: t.upi ?? 0,
        wallet: t.crypto_wallet ?? 0,
        account: t.bank_account ?? 0,
      })
      const seen = new Set<string>()
      const mixed: RailItem[] = []
      for (const r of [...(wallets.items || []), ...(accounts.items || []), ...(all.items || [])]) {
        const k = `${r.rail_type}:${r.rail_id}`
        if (!r.rail_id || seen.has(k)) continue
        seen.add(k)
        mixed.push(r)
        if (mixed.length >= 8) break
      }
      setAlerts(mixed)
    }).catch(() => {})
  }, [])
  useEffect(refresh, [refresh])
  useEffect(() => {
    const t = setInterval(() => { if (!tabHidden()) refresh() }, 60000)
    return () => clearInterval(t)
  }, [refresh])

  return (
    <>
    {!alertsOff && alerts.length > 0 && (
      <div className="money-error" style={{ marginBottom: 12 }} title="Candidate rails — analyst conclusion, not attribution">
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
          <b>
            {alerts.length > 0
              ? `⚠ ${high24 ?? alerts.length} high-priority receiving rail${(high24 ?? alerts.length) === 1 ? '' : 's'} in 24h — corroborate in Mule rails`
              : 'No high-priority rails received a deposit in the last 24h'}
          </b>
          <div style={{ display: 'flex', gap: 8 }}>
            <button className="btn" onClick={gotoMuleRails}>Open Mule rails</button>
            <button className="btn" onClick={() => setAlertsOff(true)}>Dismiss</button>
          </div>
        </div>
        {alerts.map((r) => (
          <div key={`${r.rail_type}:${r.rail_id}`} className="tiny" style={{ marginTop: 4 }}>
            <span className={`mule-priority priority-${r.priority}`}>{r.priority} · {r.candidate_score}</span>{' '}
            <span className="mono">{r.rail_id}</span>{' '}
            <span className="dim">
              {railKind(r.rail_type)} · {r.gamblers} gamblers · {r.deposit_transactions} deposits
              {r.deposit_amount_inr ? ` · ${fmtINR(r.deposit_amount_inr)}` : ''}
              {' · last '}{fmtTime(r.last_seen || r.first_seen)}
              {r.account_holders ? ` · ${r.account_holders}` : ''}
            </span>
          </div>
        ))}
      </div>
    )}
    <section className="st-cards ov-briefing" style={{ marginBottom: 16 }} title="Officer briefing — last 24h live bucket evidence">
      <div className="st-card st-card-accent">
        <div className="st-card-label">Live deposits · 24h · history from Jul 22, 2026</div>
        <div className="st-card-value accent">{(dep?.transactions ?? 0).toLocaleString()} txns</div>
        <div className="tiny dim money">{fmtINR(dep?.sum_amount_inr)} · {(dep?.transactions ?? 0) === 0 ? `source quiet since ${fmtTime(dep?.src_newest)}` : `newest slip ${fmtTime(dep?.src_newest)}`}</div>
      </div>
      <div className="st-card st-card-money">
        <div className="st-card-label">Live withdrawals · 24h</div>
        <div className="st-card-value money">{(wd?.transactions ?? 0).toLocaleString()} txns</div>
        <div className="tiny dim money">{fmtINR(wd?.sum_amount_inr)} · {(wd?.transactions ?? 0) === 0 ? `source quiet since ${fmtTime(wd?.src_newest)}` : `newest slip ${fmtTime(wd?.src_newest)}`}</div>
      </div>
      <div className="st-card st-card-warn" style={{ cursor: 'pointer' }} onClick={gotoMuleRails} title="Open Mule rails">
        <div className="st-card-label">High-priority receiving · 24h</div>
        <div className="st-card-value warn">{high24 ?? '—'}</div>
        <div className="tiny dim">UPIs / wallets / accounts that received a deposit in the last 24h · candidates, not attribution</div>
      </div>
      <div className="st-card" style={{ cursor: 'pointer' }} onClick={gotoMuleRails} title="Open Mule rails">
        <div className="st-card-label">24h mix · UPI / wallet / account</div>
        <div className="st-card-value">{mix.upi} / {mix.wallet} / {mix.account}</div>
        <div className="tiny dim">High-priority rails that received in the last 24h — not lifetime wallet census</div>
      </div>
    </section>
    </>
  )
}
