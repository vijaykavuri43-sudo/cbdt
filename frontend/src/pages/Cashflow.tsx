import { useCallback, useEffect, useState } from 'react'
import { api } from '../lib/api'
import { fmtINR } from '../lib/format'

interface DayRow { day: string; files: number; cash_in: number; cash_out: number }
interface SenderRow { sender: string; txns: number; cash: number }
interface Summary {
  by_type: { type: string; txns: number; cash: number; bonus: number }[]
  by_day: DayRow[]; by_sender: SenderRow[]
}

// Corrected cash flow: deposits vs withdrawals, ex-bonus, from operator
// ledger. For rail-level (mule) attribution see Mule Money Flows.
export function Cashflow() {
  const [data, setData] = useState<Summary | null>(null)
  const [error, setError] = useState('')
  const refresh = useCallback(() => {
    setError('')
    // 60s budget (2026-09-16): a cold-cache rebuild after a restart scans
    // ~660k ledger rows and exceeds the default 25s timeout.
    api<Summary>('/api/ledger/summary', undefined, 60000).then(setData).catch((e) => setError(String(e)))
  }, [])
  useEffect(refresh, [refresh])

  const get = (t: string) => (data?.by_type ?? []).find((r) => r.type === t)
  const cashIn = get('Deposit')?.cash ?? 0
  const reqIn = get('DepositRequest')?.cash ?? 0
  const cashOut = (get('Withdraw')?.cash ?? 0) + (get('Redeem')?.cash ?? 0)
  const bonus = get('Deposit')?.bonus ?? 0
  return (
    <>
      <div className="pg-header">
        <div className="pg-header-left"><span className="pg-header-icon">⇄</span>
          <div><h1 className="pg-header-title">Cash Flow (ex-bonus)</h1>
            <div className="pg-header-sub">Deposits vs withdrawals, ex-bonus · index more files from the Ledger tab</div></div>
        </div>
        <div className="pg-header-right"><button className="btn" onClick={refresh}>↻ Refresh</button></div>
      </div>
      {error && <div className="money-error">Could not load cash flow: {error}</div>}
      <div className="st-cards money-cards">
        <div className="st-card st-card-accent"><div className="st-card-label">Cash in (ledger)</div><div className="st-card-value accent money">{fmtINR(cashIn)}</div><div className="tiny dim">{(get('Deposit')?.txns ?? 0).toLocaleString()} credits · bonus {fmtINR(bonus)} stripped</div></div>
        <div className="st-card st-card-money"><div className="st-card-label">Cash out (ledger)</div><div className="st-card-value money">{fmtINR(cashOut)}</div><div className="tiny dim">Withdraw + Redeem</div></div>
        <div className="st-card st-card-warn"><div className="st-card-label">Net retained</div><div className="st-card-value warn money">{fmtINR(cashIn - cashOut)}</div><div className="tiny dim">request queue adds {fmtINR(reqIn)} ({(get('DepositRequest')?.txns ?? 0).toLocaleString()} rows)</div></div>
      </div>
      <section className="money-section"><div className="money-section-head"><strong>Daily cash (by export date)</strong></div>
        <div className="dp-table-wrap"><table className="dp-table"><thead><tr><th>Day (UTC)</th><th className="num">Files</th><th className="num">Cash in</th><th className="num">Cash out</th><th className="num">Net</th></tr></thead>
          <tbody>{(data?.by_day ?? []).map((r) => <tr key={r.day}><td className="mono">{r.day}</td><td className="num">{r.files}</td><td className="num money">{fmtINR(r.cash_in)}</td><td className="num money">{fmtINR(r.cash_out)}</td><td className="num money">{fmtINR(r.cash_in - r.cash_out)}</td></tr>)}</tbody></table></div>
      </section>
      <section className="money-section"><div className="money-section-head"><strong>Top crediting branches (sender)</strong></div>
        <div className="dp-table-wrap"><table className="dp-table"><thead><tr><th>Branch / sender</th><th className="num">Credits</th><th className="num">Cash</th></tr></thead>
          <tbody>{(data?.by_sender ?? []).map((r) => <tr key={r.sender}><td className="mono">{r.sender}</td><td className="num">{r.txns.toLocaleString()}</td><td className="num money">{fmtINR(r.cash)}</td></tr>)}</tbody></table></div>
      </section>
    </>
  )
}
