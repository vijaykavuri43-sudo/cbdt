import { useCallback, useEffect, useState } from 'react'
import { api, exportRailsUrl } from '../lib/api'
import { arkhamAddressUrl, bestTronscanUrl } from '../lib/explorers'
import { fmtINR, previewJoined } from '../lib/format'

type RailType = '' | 'upi' | 'bank_account' | 'crypto_wallet' | 'unknown'

interface MuleRail {
  rail_type: string
  rail_id: string
  account_holders: string
  ifscs: string
  networks: string
  utrs?: string
  utr_count?: number
  deposit_transactions: number
  withdrawal_transactions: number
  deposit_amount_inr: number
  withdrawal_amount_inr: number
  deposit_gamblers: number
  withdrawal_gamblers: number
  object_evidence_rows: number
  first_seen: string
  last_seen: string
  known_indicator: boolean
  candidate_score: number
  priority: 'high' | 'medium' | 'lead'
  assessment_fact_type: 'analyst_conclusion'
}

interface MuleResp {
  total: number
  summary: {
    candidate_rails: number
    both_direction_rails: number
    priority_counts: Record<string, number>
  }
  items: MuleRail[]
}

const railLabels: Record<string, string> = {
  '': 'All rails',
  upi: 'UPI IDs',
  bank_account: 'Bank accounts',
  crypto_wallet: 'Crypto wallets',
  unknown: 'Unresolved',
}

export function Deposits() {
  const [railType, setRailType] = useState<RailType>('')
  const [query, setQuery] = useState('')
  const [queryInput, setQueryInput] = useState('')
  const [mules, setMules] = useState<MuleResp | null>(null)
  const [error, setError] = useState('')
  const [updated, setUpdated] = useState('')

  const refresh = useCallback(() => {
    setError('')
    return api<MuleResp>('/api/mule-rails', {
      rail_type: railType === 'unknown' ? '' : railType,
      q: query,
      limit: 200,
      offset: 0,
    }, 90000)
      .then((muleRails) => {
        setMules(muleRails)
        setUpdated(new Date().toLocaleTimeString())
      })
      .catch((e) => setError(String(e)))
  }, [railType, query])

  useEffect(() => { refresh() }, [refresh])

  function setRail(next: RailType) {
    setRailType(next)
    setMules(null)
  }

  const railCol =
    railType === 'bank_account' ? 'Account number'
    : railType === 'crypto_wallet' ? 'Wallet address'
    : railType === 'upi' ? 'UPI / VPA'
    : 'Rail'

  function exportFullRailsCsv() {
    const a = document.createElement('a')
    a.href = exportRailsUrl()
    a.download = `suspected-mule-rails-${new Date().toISOString().slice(0, 10)}.csv`
    a.click()
  }

  return (
    <>
      <div className="pg-header">
        <div className="pg-header-left">
          <span className="pg-header-icon">₹</span>
          <div>
            <h1 className="pg-header-title">Suspected mule rails</h1>
            <div className="pg-header-sub">
              Unique UPI / bank / wallet rails ranked for freeze and notice.
              Slip files by folder live under Date range. Ledger cash lives under Cash flow.
            </div>
          </div>
        </div>
        <div className="pg-header-right">
          <button className="btn" onClick={refresh}>↻ Refresh</button>
          <button className="btn" onClick={exportFullRailsCsv} title="One unique-rail CSV for LE exhibits">
            ⬇ Export unique rails CSV
          </button>
        </div>
      </div>

      <div className="lf-controls money-controls">
        <form
          className="money-search"
          onSubmit={(e) => {
            e.preventDefault()
            setQuery(queryInput.trim())
          }}
        >
          <input
            value={queryInput}
            onChange={(e) => setQueryInput(e.target.value)}
            placeholder="UPI, account, wallet, holder, IFSC…"
          />
          <button className="btn" type="submit">Search</button>
          {query && (
            <button className="btn" type="button" onClick={() => { setQuery(''); setQueryInput('') }}>
              Clear
            </button>
          )}
        </form>
      </div>

      <div className="money-rail-tabs">
        {(Object.keys(railLabels) as RailType[]).map((r) => (
          <button key={r || 'all'} className={`ex-toggle ${railType === r ? 'ex-toggle-on' : ''}`} onClick={() => setRail(r)}>
            {railLabels[r]}
          </button>
        ))}
      </div>

      <div className="st-cards money-cards">
        <div className="st-card st-card-accent">
          <div className="st-card-label">Candidate rails</div>
          <div className="st-card-value accent">{(mules?.summary.candidate_rails ?? 0).toLocaleString()}</div>
          {updated && <div className="tiny dim">updated {updated}</div>}
        </div>
        <div className="st-card st-card-money">
          <div className="st-card-label">High-priority</div>
          <div className="st-card-value money">{(mules?.summary.priority_counts?.high ?? 0).toLocaleString()}</div>
        </div>
        <div className="st-card st-card-warn">
          <div className="st-card-label">Both directions</div>
          <div className="st-card-value warn">{(mules?.summary.both_direction_rails ?? 0).toLocaleString()}</div>
        </div>
      </div>
      {error && <div className="money-error">Could not load mule rails: {error}</div>}

      <section className="money-section mule-panel">
        <div className="money-section-head">
          <div>
            <strong>Unique rails</strong>
            <div className="tiny dim">Priority is an analyst triage score, not criminal attribution. Use Date range to pull the matching slip files.</div>
          </div>
        </div>
        <div className="dp-table-wrap mule-table">
          <table className="dp-table">
            <thead>
              <tr>
                <th>Priority</th>
                <th>{railCol}</th>
                <th>UTR</th>
                <th>Holder / routing</th>
                <th>Gamblers → rail</th>
                <th>Rail → gamblers</th>
                <th>Seen</th>
              </tr>
            </thead>
            <tbody>
              {(mules?.items ?? []).map((r) => {
                const utrs = previewJoined(r.utrs)
                return (
                <tr key={`${r.rail_type}:${r.rail_id}`}>
                  <td>
                    <span className={`mule-priority priority-${r.priority}`}>{r.priority} · {r.candidate_score}</span>
                    {r.known_indicator && <div className="tiny known-indicator">matches master indicator</div>}
                  </td>
                  <td>
                    <div className="mono money-rail">{r.rail_id}</div>
                    {r.rail_type === 'crypto_wallet' && r.rail_id && (
                      <div className="tiny">
                        <a href={arkhamAddressUrl(r.rail_id)} target="_blank" rel="noreferrer">Arkham ↗</a>
                        {' · '}
                        <a href={bestTronscanUrl(r.rail_id) ?? '#'} target="_blank" rel="noreferrer">Tronscan ↗</a>
                      </div>
                    )}
                    <span className="money-badge">{railLabels[r.rail_type] || r.rail_type}</span>
                  </td>
                  <td className="mono small" title={utrs.title || undefined}>
                    {utrs.text || '—'}
                    {utrs.extra > 0 ? <span className="tiny dim"> +{utrs.extra}</span> : null}
                  </td>
                  <td>
                    <div>{r.account_holders || '—'}</div>
                    <div className="tiny dim mono">{r.ifscs || r.networks || ''}</div>
                  </td>
                  <td>
                    <b>{r.deposit_transactions.toLocaleString()} txns</b> · {r.deposit_gamblers.toLocaleString()} gamblers
                    <div className="money">{fmtINR(r.deposit_amount_inr)}</div>
                  </td>
                  <td>
                    <b>{r.withdrawal_transactions.toLocaleString()} txns</b> · {r.withdrawal_gamblers.toLocaleString()} gamblers
                    <div className="money">{fmtINR(r.withdrawal_amount_inr)}</div>
                  </td>
                  <td className="tiny dim">{r.first_seen || '—'} → {r.last_seen || '—'}</td>
                </tr>
                )
              })}
              {mules && mules.items.length === 0 && (
                <tr><td colSpan={7} className="dim">No candidate rails match this filter.</td></tr>
              )}
            </tbody>
          </table>
        </div>
      </section>
    </>
  )
}
