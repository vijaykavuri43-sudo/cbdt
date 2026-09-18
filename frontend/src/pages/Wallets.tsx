import { useCallback, useEffect, useRef, useState } from 'react'
import { api, createRefreshGuard, eventsUrl, exportWalletsUrl, objectUrl, post, tabHidden } from '../lib/api'
import { arkhamAddressUrl, bestTronscanUrl, tronscanLabel } from '../lib/explorers'
import { fmtMoney } from '../lib/format'

interface WalletRow {
  key: string
  last_modified: string
  address?: string
  network?: string
  standard?: string
  confidence?: string
  checksum?: string | null
  ocr_recovered?: boolean
  amount_inr?: number | null
  amount_value?: number | null
  amount_currency?: string | null
  txids?: string[] | string | null
  hits?: number
  first_seen?: string
  last_seen?: string
}

interface WalletsResp {
  items: WalletRow[]
  wallet_hits_total: number
  distinct_wallets: number
  by_network: { network: string; count: number; hits?: number }[]
  extraction_status?: { pending?: number; pending_live?: number; slip_backlog?: number; pending_full_scan?: number }
  source?: string
  total?: number
}

function originShort(key: string): string {
  const parts = (key || '').split('/').filter(Boolean)
  if (parts.length <= 1) return key || '—'
  return `${parts[0]}/${parts[parts.length - 1]}`
}

interface FullScanStatus {
  running: boolean
  scanned: number
  found: number
  errors: number
  started_utc: string | null
  last_key: string | null
  limit: number
  rate_per_sec: number
  pending_full_scan: number
  pending_live: number
  gcs_bucket: string
  gcs_prefix: string
}

export function Wallets() {
  const PAGE_SIZE = 500
  const [data, setData] = useState<WalletsResp | null>(null)
  const [scan, setScan] = useState<FullScanStatus | null>(null)
  const [page, setPage] = useState(0)
  const [error, setError] = useState('')
  const [q, setQ] = useState('')
  const [qDebounced, setQDebounced] = useState('')
  const [network, setNetwork] = useState('')
  const [sort, setSort] = useState('recent')

  const refresh = useCallback(() => {
    setError('')
    return api<WalletsResp>('/api/wallets', {
      limit: PAGE_SIZE,
      offset: page * PAGE_SIZE,
      distinct: 1,
      q: qDebounced,
      network,
      sort,
    }, 60000)
      .then(setData)
      .catch((e) => setError(String(e)))
  }, [page, qDebounced, network, sort])

  const refreshScan = useCallback(() => {
    api<FullScanStatus>('/api/wallets/full-scan/status').then(setScan).catch(() => {})
  }, [])

  useEffect(() => { refresh() }, [refresh])
  useEffect(() => {
    const id = setTimeout(() => setQDebounced(q.trim()), 400)
    return () => clearTimeout(id)
  }, [q])
  useEffect(() => { setPage(0) }, [qDebounced, network, sort])

  const guard = useRef(createRefreshGuard(20000))
  useEffect(() => {
    refreshScan()
    const id = setInterval(() => { if (!tabHidden()) refreshScan() }, 4000)
    let es: EventSource | null = null
    try {
      es = new EventSource(eventsUrl())
      es.onmessage = (ev) => {
        try {
          const j = JSON.parse(ev.data)
          if (j.type === 'wallet_scan_progress' || j.type === 'wallet_scan_done' || j.type === 'enriched') {
            refreshScan()
            if (j.type === 'wallet_scan_done' || j.type === 'enriched') guard.current.nudge(refresh)
          }
        } catch {}
      }
    } catch {}
    return () => {
      clearInterval(id)
      if (es) es.close()
    }
  }, [refresh, refreshScan])

  async function startFullScan() {
    if (!confirm('Scan remaining bucket images for wallet QR / slip addresses? This can take a while.')) return
    try {
      await post('/api/wallets/full-scan?limit=50000&concurrency=6')
      refreshScan()
    } catch (e: any) {
      alert(e?.message || String(e))
    }
  }

  const rows = (data?.items ?? []).filter((i) => (i.key || '').trim() !== '')
  const total = data?.total ?? data?.distinct_wallets ?? rows.length
  const pageCount = Math.max(1, Math.ceil(total / PAGE_SIZE))
  const from = total === 0 ? 0 : page * PAGE_SIZE + 1
  const to = Math.min(total, page * PAGE_SIZE + rows.length)

  function exportUniqueCsv() {
    const a = document.createElement('a')
    a.href = exportWalletsUrl()
    a.download = `crypto-wallets-unique-${new Date().toISOString().slice(0, 10)}.csv`
    a.click()
  }

  return (
    <>
      <div className="pg-header">
        <div className="pg-header-left">
          <span className="pg-header-icon">₿</span>
          <div>
            <h1 className="pg-header-title">Crypto wallets</h1>
            <div className="pg-header-sub">
              One row per distinct address decoded from bucket images. Date range → Crypto wallets unique uses this same census (plus any OCR-only rails).
            </div>
          </div>
        </div>
        <div className="pg-header-right">
          <button className="btn" onClick={refresh} style={{ fontSize: 11 }}>↻ Refresh</button>
          <button
            className="btn"
            onClick={startFullScan}
            style={{ fontSize: 11 }}
            disabled={!!scan?.running}
          >
            {scan?.running ? `Scanning ${scan.scanned.toLocaleString()}` : 'Scan remaining images'}
          </button>
          <button className="btn" onClick={exportUniqueCsv} title="Distinct addresses with source file + dashboard link" style={{ fontSize: 11 }}>
            ⬇ Export unique wallets CSV
          </button>
        </div>
      </div>

      {scan?.running && (
        <div className="dp-table-wrap" style={{ marginBottom: 14 }}>
          <div style={{ padding: '8px 12px', fontSize: 11 }}>
            Scanning {scan.gcs_bucket}/{scan.gcs_prefix} · {scan.scanned.toLocaleString()} / {scan.limit.toLocaleString()} · found {scan.found}
          </div>
        </div>
      )}

      <div className="st-cards" style={{ marginBottom: 14 }}>
        <div className="st-card st-card-accent">
          <div className="st-card-label">Distinct addresses</div>
          <div className="st-card-value accent">{(data?.distinct_wallets ?? 0).toLocaleString()}</div>
          <div className="st-card-sub">
            {(data?.wallet_hits_total ?? 0).toLocaleString()} image hits · Date range unique Crypto matches this list when folder is All
          </div>
        </div>
      </div>

      {!!data?.extraction_status?.pending_full_scan && (
        <div className="tiny dim" style={{ marginBottom: 10 }}>
          Wallet QR scan still has {data.extraction_status.pending_full_scan.toLocaleString()} images queued — decode is running, not stuck.
        </div>
      )}

      {data && data.by_network.length > 0 && (
        <div style={{ marginBottom: 12 }} className="small dim">
          {data.by_network.map((n) => (
            <span key={n.network} className="mono" style={{ marginRight: 12 }}>
              {n.network}: <b>{n.count.toLocaleString()}</b> addresses
              {n.hits != null ? <span className="dim"> · {n.hits.toLocaleString()} images</span> : null}
            </span>
          ))}
        </div>
      )}

      <div className="lf-controls">
        <div className="lf-controls-left">
          <input
            value={q}
            onChange={(e) => setQ(e.target.value)}
            placeholder="Search address / source file…"
            style={{ fontSize: 11, padding: '4px 8px', borderRadius: 6, border: '1px solid var(--border, #444)', background: 'transparent', color: 'inherit', minWidth: 200 }}
          />
          <select
            value={network}
            onChange={(e) => setNetwork(e.target.value)}
            style={{ fontSize: 11, padding: '4px 6px', borderRadius: 6, border: '1px solid var(--border, #444)', background: 'transparent', color: 'inherit' }}
          >
            <option value="">All networks</option>
            {(data?.by_network ?? []).map((n) => (
              <option key={n.network} value={n.network}>{n.network} ({n.count.toLocaleString()} addresses)</option>
            ))}
          </select>
          <select
            value={sort}
            onChange={(e) => setSort(e.target.value)}
            style={{ fontSize: 11, padding: '4px 6px', borderRadius: 6, border: '1px solid var(--border, #444)', background: 'transparent', color: 'inherit' }}
          >
            <option value="recent">Recent first</option>
            <option value="hits">Most hits</option>
            <option value="amount">Largest amount</option>
          </select>
          {(qDebounced || network) && (
            <button className="ex-toggle" onClick={() => { setQ(''); setQDebounced(''); setNetwork('') }}>✕ clear</button>
          )}
        </div>
      </div>
      {error && <div className="money-error">Could not load crypto wallets: {error} <button className="btn" style={{ marginLeft: 8 }} onClick={refresh}>Retry</button></div>}

      <div className="dp-table-wrap">
        <table className="dp-table">
          <thead>
            <tr>
              <th>Address</th>
              <th>Network</th>
              <th>Hits</th>
              <th>Source file</th>
              <th>Amount</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => (
              <tr key={r.key + (r.address ?? '')}>
                <td className="mono" style={{ fontWeight: 700, wordBreak: 'break-all' }}>
                  {r.address}{r.checksum === 'invalid' && <span title="Checksum failed — OCR-degraded"> ⚠</span>}
                </td>
                <td>{r.network ?? '—'}</td>
                <td>{r.hits != null ? `${r.hits}×` : '—'}</td>
                <td className="small dim mono" style={{ maxWidth: 280, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }} title={r.key || ''}>
                  {originShort(r.key ?? '')}
                </td>
                <td className="small dim">
                  {(r.amount_currency && r.amount_currency.toUpperCase() !== 'INR')
                    ? fmtMoney(r.amount_value ?? r.amount_inr, r.amount_currency)
                    : '—'}
                </td>
                <td style={{ whiteSpace: 'nowrap' }}>
                  <a href={objectUrl(r.key)} target="_blank" rel="noreferrer" className="small">view source</a>
                  {r.address && (
                    <>
                      {' · '}
                      <a href={arkhamAddressUrl(r.address)} target="_blank" rel="noreferrer" className="small">Arkham ↗</a>
                      {' · '}
                      <a href={bestTronscanUrl(r.address, r.txids) ?? '#'} target="_blank" rel="noreferrer" className="small">{tronscanLabel(r.txids)}</a>
                    </>
                  )}
                </td>
              </tr>
            ))}
            {rows.length === 0 && (
              <tr>
                <td colSpan={6} className="dp-empty">
                  <div className="dp-empty-icon">₿</div>
                  <div style={{ fontWeight: 700, marginBottom: 4 }}>No distinct wallets on this page</div>
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>

      <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginTop: 12 }}>
        <button className="btn" disabled={page === 0} onClick={() => setPage((p) => Math.max(0, p - 1))}>← Prev</button>
        <button className="btn" disabled={page + 1 >= pageCount} onClick={() => setPage((p) => p + 1)}>Next →</button>
        <span className="small dim">
          {from.toLocaleString()}–{to.toLocaleString()} of {total.toLocaleString()} unique wallets
        </span>
      </div>
    </>
  )
}
