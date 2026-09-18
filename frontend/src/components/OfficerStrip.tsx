import { useCallback, useEffect, useState } from 'react'
import { api, tabHidden } from '../lib/api'
import { fmtINR, fmtIST } from '../lib/format'

interface Flows {
  transactions: number
  sum_amount_inr: number
  src_newest?: string | null
  window_newest?: string | null
}
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
  window_deposit_amount_inr?: number
  window_deposit_transactions?: number
  window_deposit_gamblers?: number
  window_last_seen?: string
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

function holderHint(s: string): string {
  const first = (s || '').split(',')[0].trim()
  if (!first || /view history|pay again/i.test(first)) return ''
  return first.length > 48 ? `${first.slice(0, 45)}…` : first
}

function lagPhrase(ts?: string | null): string {
  if (!ts) return ''
  const ms = Date.now() - new Date(ts).getTime()
  if (!Number.isFinite(ms)) return ''
  if (ms < 0) return 'clock ahead of bucket'
  if (ms < 2 * 60_000) return 'in sync'
  const min = Math.round(ms / 60_000)
  if (min < 60) return `pipeline lag ${min} min`
  const h = Math.floor(min / 60)
  const m = min % 60
  return m ? `pipeline lag ${h}h ${m}m` : `pipeline lag ${h}h`
}

function flowFoot(f: Flows | null, empty: boolean): string {
  const newest = empty ? f?.src_newest : (f?.window_newest || f?.src_newest)
  const lag = lagPhrase(newest)
  if (empty) {
    return `${fmtINR(f?.sum_amount_inr)} · no slips in this 24h · last ever ${fmtIST(f?.src_newest)}${lag ? ` · ${lag}` : ''}`
  }
  return `${fmtINR(f?.sum_amount_inr)} · newest ${fmtIST(newest)}${lag ? ` · ${lag}` : ''}`
}

// Officer briefing: last-24h live money + rails that received a deposit
// in the last 24h, ranked by inflow (UPI / wallet / account as they appear).
export function OfficerStrip() {
  const [dep, setDep] = useState<Flows | null>(null)
  const [wd, setWd] = useState<Flows | null>(null)
  const [high24, setHigh24] = useState<number | null>(null)
  const [mix, setMix] = useState({ upi: 0, wallet: 0, account: 0 })
  const [alerts, setAlerts] = useState<RailItem[]>([])
  const [alertsOff, setAlertsOff] = useState(false)

  const [cutoff, setCutoff] = useState('')

  const refresh = useCallback(() => {
    const nextCutoff = new Date(Date.now() - 864e5).toISOString().slice(0, 19) + 'Z'
    setCutoff(nextCutoff)
    api<Flows>('/api/money-flows', { direction: 'deposit', source: 'object', since: nextCutoff, limit: 1 }).then(setDep).catch(() => {})
    api<Flows>('/api/money-flows', { direction: 'withdrawal', source: 'object', since: nextCutoff, limit: 1 }).then(setWd).catch(() => {})
    api<RailsList>('/api/mule-rails', {
      limit: 8,
      source: 'object',
      active_since: nextCutoff,
    }).then((r) => {
      setHigh24(r.total ?? 0)
      setAlerts(r.items ?? [])
      const t = r.summary?.by_rail_type || {}
      setMix({
        upi: t.upi ?? 0,
        wallet: t.crypto_wallet ?? 0,
        account: t.bank_account ?? 0,
      })
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
              ? `⚠ ${high24 ?? alerts.length} rail${(high24 ?? alerts.length) === 1 ? '' : 's'} received deposits in 24h — top ${alerts.length} by inflow`
              : 'No rails received a deposit in the last 24h'}
          </b>
          <div style={{ display: 'flex', gap: 8 }}>
            <button className="btn" onClick={gotoMuleRails}>Open Mule rails</button>
            <button className="btn" onClick={() => setAlertsOff(true)}>Dismiss</button>
          </div>
        </div>
        {alerts.map((r) => {
          const inflowAmt = r.window_deposit_amount_inr ?? r.deposit_amount_inr
          const inflowTx = r.window_deposit_transactions ?? r.deposit_transactions
          const inflowGamblers = r.window_deposit_gamblers ?? r.gamblers
          const last = r.window_last_seen || r.last_seen || r.first_seen
          const holder = holderHint(r.account_holders)
          return (
          <div key={`${r.rail_type}:${r.rail_id}`} className="tiny" style={{ marginTop: 4 }}>
            <span className={`mule-priority priority-${r.priority}`}>{railKind(r.rail_type)}</span>{' '}
            <span className="mono">{r.rail_id}</span>{' '}
            <span className="dim">
              {inflowTx} deposits
              {inflowAmt ? ` · ${fmtINR(inflowAmt)}` : ''}
              {inflowGamblers ? ` · ${inflowGamblers} gamblers` : ''}
              {' · last '}{fmtIST(last)}
              {holder ? ` · ${holder}` : ''}
            </span>
          </div>
          )
        })}
      </div>
    )}
    <section className="st-cards ov-briefing" style={{ marginBottom: 16 }} title="Officer briefing — last 24h live bucket evidence">
      <div className="st-card st-card-accent" title="Last 24 hours from now. Bucket last-modified is UTC; times below are IST (phone/slip local). Jul 22 is the case start on the cards further down, not this window.">
        <div className="st-card-label">Live deposits · last 24 hours</div>
        <div className="st-card-value accent">{(dep?.transactions ?? 0).toLocaleString()} txns</div>
        <div className="tiny dim money">{cutoff ? `${fmtIST(cutoff)} → now IST` : 'window loading…'}</div>
        <div className="tiny dim money">{flowFoot(dep, (dep?.transactions ?? 0) === 0)}</div>
      </div>
      <div className="st-card st-card-money" title="Last 24 hours from now. Times are IST.">
        <div className="st-card-label">Live withdrawals · last 24 hours</div>
        <div className="st-card-value money">{(wd?.transactions ?? 0).toLocaleString()} txns</div>
        <div className="tiny dim money">{cutoff ? `${fmtIST(cutoff)} → now IST` : 'window loading…'}</div>
        <div className="tiny dim money">{flowFoot(wd, (wd?.transactions ?? 0) === 0)}</div>
      </div>
      <div className="st-card st-card-warn" style={{ cursor: 'pointer' }} onClick={gotoMuleRails} title="Open Mule rails">
        <div className="st-card-label">Receiving rails · 24h inflow</div>
        <div className="st-card-value warn">{high24 ?? '—'}</div>
        <div className="tiny dim">Distinct UPIs / wallets / accounts that received a deposit in the last 24h — ranked by rupees in, not a fixed list</div>
      </div>
      <div className="st-card" style={{ cursor: 'pointer' }} onClick={gotoMuleRails} title="Open Mule rails">
        <div className="st-card-label">24h mix · UPI / wallet / account</div>
        <div className="st-card-value">{mix.upi} / {mix.wallet} / {mix.account}</div>
        <div className="tiny dim">Live count of who received in the last 24h — updates with inflow, not a hardcoded quota</div>
      </div>
    </section>
    </>
  )
}
