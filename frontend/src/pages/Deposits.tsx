import { useCallback, useEffect, useMemo, useState } from 'react'
import { api, eventsUrl, objectUrl, tabHidden } from '../lib/api'
import { fmtINR, fmtTime, shortKey } from '../lib/format'

type Direction = 'deposit' | 'withdrawal'
type RailType = '' | 'upi' | 'bank_account' | 'crypto_wallet' | 'unknown'
type SourceFilter = '' | 'object' | 'reference'

interface FlowRow { source_key: string; direction: Direction; gambler: string; amount_inr?: number | null; utr: string; rail_type: string; rail_id: string; account_holder: string; ifsc: string; network: string; confidence: string; rail_role: string; role_confidence: string; last_modified: string; has_object: boolean }
interface TopRail { rail_type: string; rail_id: string; account_holder: string; ifsc: string; network: string; transactions: number; amount_inr: number; gamblers: number }
interface FlowResp { direction: Direction; transactions: number; sum_amount_inr: number; distinct_rails: number; distinct_gamblers: number; gambler_linked_transactions: number; rows_total: number; top_rails: TopRail[]; items: FlowRow[] }
interface MuleRail { rail_type: string; rail_id: string; account_holders: string; ifscs: string; networks: string; transactions: number; deposit_transactions: number; withdrawal_transactions: number; deposit_amount_inr: number; withdrawal_amount_inr: number; gamblers: number; deposit_gamblers: number; withdrawal_gamblers: number; high_role_evidence: number; medium_role_evidence: number; low_role_evidence: number; object_evidence_rows: number; reference_rows: number; first_seen: string; last_seen: string; known_indicator: boolean; candidate_score: number; priority: 'high' | 'medium' | 'lead'; fact_type: 'fact'; assessment_fact_type: 'analyst_conclusion' }
interface MuleResp { total: number; summary: { candidate_rails: number; receiving_rails: number; sending_rails: number; both_direction_rails: number; priority_counts: Record<string, number> }; items: MuleRail[]; scope_note: string; scoring_note: string }

const railLabels: Record<string, string> = { '': 'All rails', upi: 'UPI IDs', bank_account: 'Bank accounts', crypto_wallet: 'Crypto wallets', unknown: 'Unresolved' }

export function Deposits() {
  const [direction, setDirection] = useState<Direction>('deposit')
  const [railType, setRailType] = useState<RailType>('')
  const [source, setSource] = useState<SourceFilter>('')
  const [query, setQuery] = useState('')
  const [queryInput, setQueryInput] = useState('')
  const [data, setData] = useState<FlowResp | null>(null)
  const [mules, setMules] = useState<MuleResp | null>(null)
  const [error, setError] = useState('')
  const [page, setPage] = useState(0)
  const [auto, setAuto] = useState(true)
  const [updated, setUpdated] = useState('')
  const limit = 200

  const refresh = useCallback(() => {
    setError('')
    Promise.all([
      api<FlowResp>('/api/money-flows', { direction, rail_type: railType, q: query, limit, offset: page * limit, ...(source ? { source } : {}) }),
      api<MuleResp>('/api/mule-rails', { rail_type: railType === 'unknown' ? '' : railType, q: query, limit: 200, offset: 0, ...(source ? { source } : {}) }),
    ])
      .then(([flows, muleRails]) => { setData(flows); setMules(muleRails); setUpdated(new Date().toLocaleTimeString()) })
      .catch((e) => setError(String(e)))
  }, [direction, railType, source, query, page])
  useEffect(refresh, [refresh])
  // Fresh OCR/QR extraction lands continuously — re-pull every 12s + instantly on SSE push
  useEffect(() => {
    if (!auto) return
    const t = setInterval(() => { if (!tabHidden()) refresh() }, 12000)
    return () => clearInterval(t)
  }, [auto, refresh])
  useEffect(() => {
    if (!auto) return
    let es: EventSource | null = null
    try {
      es = new EventSource(eventsUrl())
      const onMsg = (e: MessageEvent) => {
        try {
          const d = JSON.parse(e.data)
          if (d?.type === 'enriched' || d?.type === 'new_object' || d?.type === 'wallet_found') refresh()
        } catch {}
      }
      es.addEventListener('message', onMsg as any)
      ;(es as any).onmessage = onMsg
    } catch {}
    return () => { try { es?.close() } catch {} }
  }, [auto, refresh])

  const coverage = data?.transactions ? Math.round((data.gambler_linked_transactions / data.transactions) * 100) : 0
  const arrow = direction === 'deposit' ? '→' : '←'
  const flowLabel = direction === 'deposit' ? 'Gambler → receiving rail' : 'Gambler ← sending rail'
  const exportRows = useMemo(() => data?.items ?? [], [data])

  function setMode(next: Direction) { setDirection(next); setPage(0); setData(null) }
  function setRail(next: RailType) { setRailType(next); setPage(0); setData(null) }
  function setSourceFilter(next: SourceFilter) { setSource(next); setPage(0); setData(null) }
  function exportTsv() {
    const header = ['Time (UTC)', 'Direction', 'Gambler', 'Amount INR', 'UTR', 'Rail type', 'UPI / account / wallet', 'Holder', 'IFSC / network', 'Confidence', 'Evidence key'].join('\t') + '\n'
    const rows = exportRows.map((r) => [r.last_modified, r.direction, r.gambler, r.amount_inr ?? '', r.utr, r.rail_type, r.rail_id, r.account_holder, r.ifsc || r.network, r.confidence, r.source_key].join('\t')).join('\n')
    const blob = new Blob([header + rows], { type: 'text/tab-separated-values' })
    const a = document.createElement('a'); a.href = URL.createObjectURL(blob); a.download = `${direction}-${railType || 'all-rails'}-${new Date().toISOString().slice(0, 10)}.tsv`; a.click(); URL.revokeObjectURL(a.href)
  }
  function exportMuleTsv() {
    const header = ['Priority', 'Candidate score', 'Rail type', 'Rail ID', 'Holders', 'IFSC/network', 'Deposit txns', 'Deposit linked INR', 'Deposit gamblers', 'Withdrawal txns', 'Withdrawal linked INR', 'Withdrawal gamblers', 'Object evidence', 'Reference evidence', 'Known indicator', 'First seen', 'Last seen', 'Assessment type'].join('\t') + '\n'
    const rows = (mules?.items ?? []).map((r) => [r.priority, r.candidate_score, r.rail_type, r.rail_id, r.account_holders || '', r.ifscs || r.networks || '', r.deposit_transactions, r.deposit_amount_inr, r.deposit_gamblers, r.withdrawal_transactions, r.withdrawal_amount_inr, r.withdrawal_gamblers, r.object_evidence_rows, r.reference_rows, r.known_indicator, r.first_seen, r.last_seen, r.assessment_fact_type].join('\t')).join('\n')
    const blob = new Blob([header + rows], { type: 'text/tab-separated-values' })
    const a = document.createElement('a'); a.href = URL.createObjectURL(blob); a.download = `suspected-mule-rails-${new Date().toISOString().slice(0, 10)}.tsv`; a.click(); URL.revokeObjectURL(a.href)
  }
  function inspectRail(r: MuleRail) { setQueryInput(r.rail_id); setQuery(r.rail_id); setRailType(r.rail_type as RailType); setPage(0) }

  return <>
    <div className="pg-header">
      <div className="pg-header-left"><span className="pg-header-icon">⇄</span><div><h1 className="pg-header-title">Suspected Mule Money Flows</h1><div className="pg-header-sub">Deposits: gambler → receiving rail · Withdrawals: sending rail → gambler · Every result preserves evidence provenance and role confidence</div></div></div>
      <div className="pg-header-right">
        <button className={`ex-toggle ${auto ? 'ex-toggle-on' : ''}`} onClick={() => setAuto(!auto)} title="Re-pull every 12s + instantly on new evidence via SSE">{auto ? '⏸ Auto 12s+live' : '▶ Auto off'}</button>
        <button className="btn" onClick={refresh}>↻ Refresh</button>
        <button className="btn" onClick={exportMuleTsv}>⬇ Export mule candidates</button>
        <button className="btn" onClick={exportTsv}>⬇ Export current page</button>
      </div>
    </div>

    <div className="lf-controls money-controls">
      <div className="lf-controls-left"><button className={`ex-toggle ${direction === 'deposit' ? 'ex-toggle-on' : ''}`} onClick={() => setMode('deposit')}>Deposits → platform</button><button className={`ex-toggle ${direction === 'withdrawal' ? 'ex-toggle-on' : ''}`} onClick={() => setMode('withdrawal')}>Withdrawals → gambler</button></div>
      <form className="money-search" onSubmit={(e) => { e.preventDefault(); setQuery(queryInput.trim()); setPage(0) }}><input value={queryInput} onChange={(e) => setQueryInput(e.target.value)} placeholder="Gambler, UTR, UPI, account, wallet, IFSC…" /><button className="btn" type="submit">Search</button>{query && <button className="btn" type="button" onClick={() => { setQuery(''); setQueryInput(''); setPage(0) }}>Clear</button>}</form>
    </div>

    <div className="money-rail-tabs">{(Object.keys(railLabels) as RailType[]).map((r) => <button key={r || 'all'} className={`ex-toggle ${railType === r ? 'ex-toggle-on' : ''}`} onClick={() => setRail(r)}>{railLabels[r]}</button>)}</div>
    <div className="money-rail-tabs" title="Live bucket evidence vs 5-day reference join"><button className={`ex-toggle ${source === '' ? 'ex-toggle-on' : ''}`} onClick={() => setSourceFilter('')}>All sources</button><button className={`ex-toggle ${source === 'object' ? 'ex-toggle-on' : ''}`} onClick={() => setSourceFilter('object')}>◉ Live evidence</button><button className={`ex-toggle ${source === 'reference' ? 'ex-toggle-on' : ''}`} onClick={() => setSourceFilter('reference')}>5-day reference</button></div>
    <div className="st-cards money-cards">
      <div className="st-card st-card-accent"><div className="st-card-label">Transactions</div><div className="st-card-value accent">{(data?.transactions ?? 0).toLocaleString()}</div></div>
      <div className="st-card st-card-money"><div className="st-card-label">Amount linked</div><div className="st-card-value money">{fmtINR(data?.sum_amount_inr)}</div></div>
      <div className="st-card"><div className="st-card-label">Distinct gamblers</div><div className="st-card-value">{(data?.distinct_gamblers ?? 0).toLocaleString()}</div></div>
      <div className="st-card st-card-warn"><div className="st-card-label">Distinct payment rails</div><div className="st-card-value warn">{(data?.distinct_rails ?? 0).toLocaleString()}</div></div>
      <div className="st-card"><div className="st-card-label">Gambler linkage</div><div className="st-card-value">{coverage}%</div><div className="tiny dim">{(data?.gambler_linked_transactions ?? 0).toLocaleString()} linked transactions{updated && <> · updated {updated}</>}</div></div>
    </div>
    {error && <div className="money-error">Could not load flows: {error}</div>}

    <section className="money-section mule-panel">
      <div className="money-section-head"><div><strong>Suspected mule rail intelligence</strong><div className="tiny dim">Observed transfers are facts. “Mule candidate” and priority are analyst conclusions for triage—not criminal attribution without corroboration.</div></div><span className="small dim">{mules?.scoring_note}</span></div>
      <div className="st-cards money-cards">
        <div className="st-card st-card-accent"><div className="st-card-label">Candidate rails</div><div className="st-card-value accent">{(mules?.summary.candidate_rails ?? 0).toLocaleString()}</div></div>
        <div className="st-card"><div className="st-card-label">Receiving from gamblers</div><div className="st-card-value">{(mules?.summary.receiving_rails ?? 0).toLocaleString()}</div></div>
        <div className="st-card"><div className="st-card-label">Sending to gamblers</div><div className="st-card-value">{(mules?.summary.sending_rails ?? 0).toLocaleString()}</div></div>
        <div className="st-card st-card-warn"><div className="st-card-label">Both directions</div><div className="st-card-value warn">{(mules?.summary.both_direction_rails ?? 0).toLocaleString()}</div></div>
        <div className="st-card st-card-money"><div className="st-card-label">High-priority triage</div><div className="st-card-value money">{(mules?.summary.priority_counts?.high ?? 0).toLocaleString()}</div></div>
      </div>
      <div className="dp-table-wrap mule-table"><table className="dp-table">
        <thead><tr><th>Assessment</th><th>UPI / account / wallet</th><th>Holder / routing</th><th>Gamblers → rail (deposit)</th><th>Rail → gamblers (withdrawal)</th><th>Evidence / observed</th><th></th></tr></thead>
        <tbody>{(mules?.items ?? []).map((r) => <tr key={`${r.rail_type}:${r.rail_id}`}><td><span className={`mule-priority priority-${r.priority}`}>{r.priority} · {r.candidate_score}</span><div className="tiny dim">mule candidate · analyst conclusion</div>{r.known_indicator && <div className="tiny known-indicator">matches master indicator</div>}</td><td><div className="mono money-rail">{r.rail_id}</div><span className="money-badge">{railLabels[r.rail_type] || r.rail_type}</span></td><td><div>{r.account_holders || '—'}</div><div className="tiny dim mono">{r.ifscs || r.networks || ''}</div></td><td><b>{r.deposit_transactions.toLocaleString()} txns</b> · {r.deposit_gamblers.toLocaleString()} gamblers<div className="money">{fmtINR(r.deposit_amount_inr)}</div></td><td><b>{r.withdrawal_transactions.toLocaleString()} txns</b> · {r.withdrawal_gamblers.toLocaleString()} gamblers<div className="money">{fmtINR(r.withdrawal_amount_inr)}</div></td><td><div>{r.object_evidence_rows.toLocaleString()} object · {r.reference_rows.toLocaleString()} reference</div><div className="tiny dim">role: {r.high_role_evidence} high · {r.medium_role_evidence} medium · {r.low_role_evidence} ambiguous</div><div className="tiny dim">{r.first_seen || '—'} → {r.last_seen || '—'}</div></td><td><button className="btn" onClick={() => inspectRail(r)}>Inspect</button></td></tr>)}{mules && mules.items.length === 0 && <tr><td colSpan={7} className="dim">No candidate rails match this filter.</td></tr>}</tbody>
      </table></div>
    </section>

    <section className="money-section"><div className="money-section-head"><strong>Top receiving/sending rails</strong><span className="small dim">Ranked by transaction count · {railLabels[railType]}</span></div><div className="dp-table-wrap"><table className="dp-table">
      <thead><tr><th>Type</th><th>UPI / account / wallet</th><th>Holder / IFSC / network</th><th className="num">Transactions</th><th className="num">Gamblers</th><th className="num">Amount</th></tr></thead>
      <tbody>{(data?.top_rails ?? []).slice(0, 25).map((r) => <tr key={`${r.rail_type}:${r.rail_id}:${r.account_holder}`}><td><span className="money-badge">{railLabels[r.rail_type] || r.rail_type}</span></td><td className="mono money-rail">{r.rail_id || 'unresolved'}</td><td><div>{r.account_holder || '—'}</div><div className="tiny dim mono">{r.ifsc || r.network || ''}</div></td><td className="num">{r.transactions.toLocaleString()}</td><td className="num">{r.gamblers.toLocaleString()}</td><td className="num money">{fmtINR(r.amount_inr)}</td></tr>)}{data && data.top_rails.length === 0 && <tr><td colSpan={6} className="dim">No resolved payment rails match this filter.</td></tr>}</tbody>
    </table></div></section>

    <section className="money-section"><div className="money-section-head"><strong>{flowLabel}</strong><span className="small dim">Each row retains its exact evidence key or reference provenance</span></div><div className="dp-table-wrap"><table className="dp-table">
      <thead><tr><th>Time</th><th>Flow</th><th className="num">Amount</th><th>UTR</th><th>Holder / routing</th><th>Confidence</th><th>Evidence</th></tr></thead>
      <tbody>{(data?.items ?? []).map((r, i) => <tr key={`${r.source_key}:${r.rail_type}:${r.rail_id}:${i}`}><td className="small dim">{r.last_modified ? fmtTime(r.last_modified) : 'reference'}</td><td><div className="money-flow"><span className={r.gambler ? '' : 'dim'}>{r.gambler || 'unlinked gambler'}</span><b>{arrow}</b><span className="mono">{r.rail_id || 'unresolved rail'}</span></div><div className="tiny dim">{railLabels[r.rail_type] || r.rail_type} · role {r.rail_role || 'unknown'} ({r.role_confidence || 'low'})</div></td><td className="num money">{r.amount_inr != null ? fmtINR(r.amount_inr) : '—'}</td><td className="mono small">{r.utr || '—'}</td><td><div>{r.account_holder || '—'}</div><div className="tiny dim mono">{r.ifsc || r.network || ''}</div></td><td><span className={`money-confidence confidence-${r.confidence || 'unknown'}`}>{r.confidence || 'reference'}</span></td><td>{r.has_object ? <a href={objectUrl(r.source_key)} target="_blank" rel="noreferrer">{shortKey(r.source_key.split('/').pop() || r.source_key, 24)}</a> : <span className="tiny dim">UTR reference join</span>}</td></tr>)}{!data && <tr><td colSpan={7} className="dim">Loading normalized payment flows…</td></tr>}{data && data.items.length === 0 && <tr><td colSpan={7} className="dim">No flows match this filter.</td></tr>}</tbody>
    </table></div></section>
    <div className="ex-pagination"><button className="ex-page-btn" disabled={page === 0} onClick={() => setPage(page - 1)}>← Prev</button><span className="ex-page-info">page {page + 1} · {(data?.rows_total ?? 0).toLocaleString()} rail rows</span><button className="ex-page-btn" disabled={(page + 1) * limit >= (data?.rows_total ?? 0)} onClick={() => setPage(page + 1)}>Next →</button></div>
  </>
}
