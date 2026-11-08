import { useCallback, useEffect, useState } from 'react'
import { api, exportPayoutsUrl, post } from '../lib/api'
import { fmtINR } from '../lib/format'

interface TypeRow { type: string; txns: number; cash: number; bonus: number; gamblers: number; utrs: number }
interface InvRow { t: string; files: number; bytes: number; newest: string }
interface Summary {
  fact_type: string; note: string
  by_type: TypeRow[]; inventory: InvRow[]
  coverage: { ledger_utrs: number; matched_in_flows: number }
  files: { indexed: number; total: number }
}
interface SyncOut { attempted: number; indexed: number; skipped: number; errors: string[]; files_indexed: number; files_total: number; synced_utc: string }

interface GwBrand { website: string; txns: number; amount: number; customers: number; utrs: number }
interface GwAccount { holder: string; account: string; ifsc: string; txns: number; amount: number; sample_txid: string }
interface Gateway {
  fact_type: string; note: string
  deposits: { rows: number; amount: number; customers: number; utrs: number; brands: number }
  test_rows_excluded: number
  brands: GwBrand[]
  payouts: { rows: number; amount: number; accounts: number }
  payout_accounts: GwAccount[]
  coverage: { gateway_utrs: number; matched_in_flows: number }
  files: { indexed: number; total: number; bucket_total: number; newest: string }
}

// Operator ledger (csv-files/ XLSX+CSV): file inventory + corrected ex-bonus
// cash. Raw Amount is chips (includes bonus) — never quoted as cash.
// Gateway section: paymentgateway/csv-files/ settlement exports by brand.
export function Ledger() {
  const [data, setData] = useState<Summary | null>(null)
  const [gw, setGw] = useState<Gateway | null>(null)
  const [syncing, setSyncing] = useState(false)
  const [gwSyncing, setGwSyncing] = useState(false)
  const [syncMsg, setSyncMsg] = useState('')
  const [gwMsg, setGwMsg] = useState('')
  const [error, setError] = useState('')

  const refresh = useCallback(() => {
    setError('')
    api<Summary>('/api/ledger/summary').then(setData).catch((e) => setError(String(e)))
    api<Gateway>('/api/gateway/summary', undefined, 90000).then(setGw).catch(() => {})
  }, [])
  useEffect(refresh, [refresh])

  function syncMore() {
    setSyncing(true); setSyncMsg('')
    post<SyncOut>('/api/ledger/sync?limit=100').then((r) => {
      setSyncMsg(`indexed ${r.indexed}/${r.attempted} · ${r.files_indexed}/${r.files_total} files · ${r.errors.length ? r.errors[0] : 'no errors'}`)
      refresh()
    }).catch((e) => setSyncMsg(String(e))).finally(() => setSyncing(false))
  }

  function exportPayoutsCsv() {
    // Full officer exhibit: every payout row with source file + dashboard link
    const a = document.createElement('a')
    a.href = exportPayoutsUrl()
    a.download = `payout-bank-accounts-full-${new Date().toISOString().slice(0, 10)}.csv`
    a.click()
  }

  function syncGateway() {    setGwSyncing(true); setGwMsg('')
    post<SyncOut>('/api/gateway/sync?limit=100').then((r) => {
      setGwMsg(`indexed ${r.indexed}/${r.attempted} · ${r.files_indexed}/${r.files_total} files · ${r.errors.length ? r.errors[0] : 'no errors'}`)
      refresh()
    }).catch((e) => setGwMsg(String(e))).finally(() => setGwSyncing(false))
  }

  const gwCov = gw && gw.coverage.gateway_utrs ? Math.round((gw.coverage.matched_in_flows / gw.coverage.gateway_utrs) * 100) : 0

  const cov = data && data.coverage.ledger_utrs ? Math.round((data.coverage.matched_in_flows / data.coverage.ledger_utrs) * 100) : 0
  return (
    <>
      <div className="pg-header">
        <div className="pg-header-left"><span className="pg-header-icon">▦</span>
          <div><h1 className="pg-header-title">Ledger (XLSX / CSV)</h1>
            <div className="pg-header-sub">Operator admin exports · cash = Amount − Bonus · {data?.note}</div></div>
        </div>
        <div className="pg-header-right">
          <button className="btn" onClick={syncMore} disabled={syncing}>{syncing ? 'Indexing…' : '⬇ Index new files (100)'}</button>
          <button className="btn" onClick={refresh}>↻ Refresh</button>
        </div>
      </div>
      {syncMsg && <div className="tiny dim" style={{ marginBottom: 8 }}>{syncMsg}</div>}
      {error && <div className="money-error">Could not load ledger: {error}</div>}
      <div className="st-cards money-cards">
        <div className="st-card st-card-accent"><div className="st-card-label">Ledger files indexed</div><div className="st-card-value accent">{(data?.files.indexed ?? 0).toLocaleString()} / {(data?.files.total ?? 0).toLocaleString()}</div></div>
        <div className="st-card st-card-money"><div className="st-card-label">Ledger UTRs slip-matched</div><div className="st-card-value money">{cov}%</div><div className="tiny dim">{(data?.coverage.matched_in_flows ?? 0).toLocaleString()} / {(data?.coverage.ledger_utrs ?? 0).toLocaleString()}</div></div>
      </div>
      <section className="money-section"><div className="money-section-head"><strong>Corrected totals by entry type</strong><span className="small dim">fact · cash ex-bonus</span></div>
        <div className="dp-table-wrap"><table className="dp-table"><thead><tr><th>Type</th><th className="num">Rows</th><th className="num">Cash (ex-bonus)</th><th className="num">Bonus</th><th className="num">Gamblers</th><th className="num">UTRs</th></tr></thead>
          <tbody>{(data?.by_type ?? []).map((r) => <tr key={r.type}><td><b>{r.type}</b></td><td className="num">{r.txns.toLocaleString()}</td><td className="num money">{fmtINR(r.cash)}</td><td className="num">{fmtINR(r.bonus)}</td><td className="num">{r.gamblers.toLocaleString()}</td><td className="num">{r.utrs.toLocaleString()}</td></tr>)}</tbody></table></div>
      </section>
      <section className="money-section"><div className="money-section-head"><strong>File inventory (bucket manifest)</strong><span className="small dim">new timestamped files daily · never overwritten</span></div>
        <div className="dp-table-wrap"><table className="dp-table"><thead><tr><th>Prefix group</th><th className="num">Files</th><th className="num">Bytes</th><th>Newest</th></tr></thead>
          <tbody>{(data?.inventory ?? []).map((r) => <tr key={r.t}><td className="mono">{r.t}</td><td className="num">{r.files.toLocaleString()}</td><td className="num">{(r.bytes / 1048576).toFixed(1)} MB</td><td className="small dim">{r.newest}</td></tr>)}</tbody></table></div>
      </section>

      <div className="pg-header" style={{ marginTop: 18 }}>
        <div className="pg-header-left"><span className="pg-header-icon">⛩</span>
          <div><h2 className="pg-header-title">Gateway settlements</h2>
            <div className="pg-header-sub">paymentgateway/csv-files/ · deposits by white-label brand · payout bank accounts · {gw?.note}</div></div>
        </div>
        <div className="pg-header-right">
          <button className="btn" onClick={syncGateway} disabled={gwSyncing}>{gwSyncing ? 'Indexing…' : '⬇ Index gateway files (100)'}</button>
        </div>
      </div>
      {gwMsg && <div className="tiny dim" style={{ marginBottom: 8 }}>{gwMsg}</div>}
      <div className="st-cards money-cards">
        <div className="st-card st-card-accent"><div className="st-card-label">Gateway files indexed</div><div className="st-card-value accent">{(gw?.files.indexed ?? 0).toLocaleString()} / {(gw?.files.bucket_total ?? 0).toLocaleString()}</div><div className="tiny dim">newest {gw?.files.newest || '—'}</div></div>
        <div className="st-card st-card-money"><div className="st-card-label">Real deposit rows</div><div className="st-card-value money">{(gw?.deposits.rows ?? 0).toLocaleString()}</div><div className="tiny dim">{fmtINR(gw?.deposits.amount)} · {(gw?.deposits.brands ?? 0)} brands · test rows excluded {(gw?.test_rows_excluded ?? 0).toLocaleString()}</div></div>
        <div className="st-card st-card-warn"><div className="st-card-label">Payout mule accounts</div><div className="st-card-value warn">{(gw?.payouts.accounts ?? 0).toLocaleString()}</div><div className="tiny dim">{(gw?.payouts.rows ?? 0).toLocaleString()} rows · {fmtINR(gw?.payouts.amount)}</div></div>
        <div className="st-card"><div className="st-card-label">Gateway UTRs slip-matched</div><div className="st-card-value">{gwCov}%</div><div className="tiny dim">{(gw?.coverage.matched_in_flows ?? 0).toLocaleString()} / {(gw?.coverage.gateway_utrs ?? 0).toLocaleString()}</div></div>
      </div>
      <section className="money-section"><div className="money-section-head"><strong>Deposits by brand (Website)</strong><span className="small dim">fact · test-pattern rows excluded</span></div>
        <div className="dp-table-wrap"><table className="dp-table"><thead><tr><th>Brand</th><th className="num">Txns</th><th className="num">Amount</th><th className="num">Gamblers</th><th className="num">UTRs</th></tr></thead>
          <tbody>{(gw?.brands ?? []).map((r) => <tr key={r.website}><td className="mono" style={{ fontWeight: 700 }}>{r.website}</td><td className="num">{r.txns.toLocaleString()}</td><td className="num money">{fmtINR(r.amount)}</td><td className="num">{r.customers.toLocaleString()}</td><td className="num">{r.utrs.toLocaleString()}</td></tr>)}{!gw && <tr><td colSpan={5} className="dim">Loading gateway settlements…</td></tr>}</tbody></table></div>
      </section>
      <section className="money-section"><div className="money-section-head"><strong>Payout bank accounts (observed)</strong><span className="small dim">fact · accounts observed in exports, not criminal attribution</span><button className="btn" onClick={exportPayoutsCsv} title="Every payout row (holder, account, IFSC, txid, amount) with source export file + dashboard link per row — officer exhibit, opens in Excel" style={{ fontSize: 11, marginLeft: 'auto' }}>⬇ Export FULL CSV (LE)</button></div>
        <div className="dp-table-wrap"><table className="dp-table"><thead><tr><th>Holder</th><th>Account</th><th>IFSC</th><th className="num">Txns</th><th className="num">Amount</th></tr></thead>
          <tbody>{(gw?.payout_accounts ?? []).slice(0, 50).map((r) => <tr key={`${r.account}:${r.ifsc}`}><td>{r.holder || '—'}</td><td className="mono">{r.account}</td><td className="mono small">{r.ifsc}</td><td className="num">{r.txns.toLocaleString()}</td><td className="num money">{fmtINR(r.amount)}</td></tr>)}</tbody></table></div>
      </section>
    </>
  )
}
