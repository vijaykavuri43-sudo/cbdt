import { useCallback, useEffect, useState } from 'react'
import { api, eventsUrl, exportWalletsUrl, objectUrl, post, tabHidden } from '../lib/api'
import { arkhamAddressUrl, bestTronscanUrl, tronscanLabel } from '../lib/explorers'
import { fmtMoney } from '../lib/format'

// Every row is one address decoded from a live bucket image (QR / slip /
// crypto chat screenshot). Origin is ALWAYS the exact S3 bucket object key —
// so every row has view proof. No external reference lists.
interface WalletRow {
  key: string
  last_modified: string
  address?: string
  network?: string
  standard?: string
  confidence?: string
  role?: string | null
  role_confidence?: string | null
  checksum?: string | null
  ocr_recovered?: boolean
  amount_inr?: number | null
  amount_value?: number | null
  amount_currency?: string | null
  txids?: string[] | string | null
  kind?: string | null
}
interface WalletsResp {
  items: WalletRow[]
  wallet_hits_total: number
  distinct_wallets: number
  by_network: { network: string; count: number }[]
  extraction_status?: { enriched: number; pending: number; objects: number; pending_full_scan?: number }
  source?: string
  distinct?: boolean
  limit?: number
  offset?: number
  total?: number
}
interface RefWallet {
  address: string
  network?: string
  category?: string
  attribution?: string
  role?: string
  confidence?: string
  evidence_type?: string
  source_ref?: string
  proof_s3_key?: string
  proof_url?: string
  fact_type?: string
  in_live?: boolean
}
interface RefResp {
  items: RefWallet[]
  identified_total: number
  live_decoded: number
  missing_from_live: number
}

// Origin shows folder + file (folder suffix), never the bare basename:
// "withdrawal/1763345228102.jpg" — full key stays in the title tooltip.
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
  source: string
  gcs_bucket: string
  gcs_prefix: string
}

export function Wallets() {
  const PAGE_SIZE = 500
  const [data, setData] = useState<WalletsResp | null>(null)
  const [scan, setScan] = useState<FullScanStatus | null>(null)
  const [distinct, setDistinct] = useState(true)
  const [page, setPage] = useState(0)
  const [error, setError] = useState('')
  const [ref, setRef] = useState<RefResp | null>(null)

  const refresh = useCallback(() => {
    // 2026-09-16: paginated — old wallets never fall off the newest-N window.
    // Distinct mode (default) gives one row per address so old single-hit
    // wallets stay reachable; All-hits mode shows every object×wallet row.
    setError('')
    api<WalletsResp>('/api/wallets', { limit: PAGE_SIZE, offset: page * PAGE_SIZE, distinct: distinct ? 1 : 0 }, 60000)
      .then(setData)
      .catch((e) => setError(String(e)))
  }, [page, distinct])
  const refreshScan = useCallback(() => {
    api<FullScanStatus>('/api/wallets/full-scan/status').then(setScan).catch(() => {})
  }, [])
  useEffect(refresh, [refresh])
  useEffect(() => { setPage(0) }, [distinct])
  useEffect(() => {
    api<RefResp>('/api/wallets/reference', {}, 60000).then(setRef).catch(() => {})
  }, [])
  useEffect(() => {
    refreshScan()
    const id = setInterval(() => { if (!tabHidden()) refreshScan() }, 3000)
    // live updates via SSE wallet_scan_progress
    let es: EventSource | null = null
    try {
      es = new EventSource(eventsUrl())
      es.onmessage = (ev) => {
        try {
          const j = JSON.parse(ev.data)
          if (j.type === 'wallet_scan_progress' || j.type === 'wallet_scan_done' || j.type === 'enriched') {
            refreshScan()
            if (j.type === 'wallet_scan_done' || j.type === 'enriched') refresh()
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
    if (!confirm(`Full-bucket wallet scan: prioritized QR → slips → all chat_file images directly against GCS s3-mirror (${data?.source ?? 'gs://cbtf-case-evidence-518405669914/s3-mirror/'}). 50k objects ≈ 20–40 min at 6 workers. Continue?`)) return
    try {
      await post('/api/wallets/full-scan?limit=50000&concurrency=6')
      refreshScan()
    } catch (e: any) {
      alert(e?.message || String(e))
    }
  }

  // Rows missing a bucket key carry no proof — keep them out of the exhibit.
  const rows = (data?.items ?? []).filter((i) => (i.key || '').trim() !== '')
  const total = data?.total ?? rows.length
  const pageCount = Math.max(1, Math.ceil(total / PAGE_SIZE))
  const from = total === 0 ? 0 : page * PAGE_SIZE + 1
  const to = Math.min(total, page * PAGE_SIZE + rows.length)

  function exportFullCsv() {
    // THE single officer exhibit: live bucket decodes + all identified
    // reference wallets, one CSV (Excel-ready). Server streams the whole
    // set with origin_kind (live/reference) per row.
    const a = document.createElement('a')
    a.href = exportWalletsUrl()
    a.download = `crypto-wallets-full-${new Date().toISOString().slice(0, 10)}.csv`
    a.click()
  }

  return (
    <>
      <div className="pg-header">
        <div className="pg-header-left">
          <span className="pg-header-icon">₿</span>
          <div>
            <h1 className="pg-header-title">Crypto Wallets</h1>
            <div className="pg-header-sub">
              Source: <span className="mono">{data?.source ?? 'gs://cbtf-case-evidence-518405669914/s3-mirror/'}</span> · TRON / EVM / BTC decoded from QR · slips · all chat_file images (full-scan) · every row = one bucket object with view proof
            </div>
          </div>
        </div>
        <div className="pg-header-right">
          <button className="btn" onClick={refresh} style={{ fontSize: 11, marginRight: 8 }}>↻ Refresh</button>
          <button className="btn" onClick={startFullScan} style={{ fontSize: 11, marginRight: 8, borderColor: scan?.running ? 'var(--accent)' : undefined, opacity: scan?.running ? 0.6 : 1 }} disabled={!!scan?.running}>{scan?.running ? `⏳ scanning ${scan.scanned.toLocaleString()}` : '◉ Full wallet scan (GCS)'}</button>
          <button className="btn" onClick={exportFullCsv} title="Every wallet in one CSV: live bucket decodes + all 114 identified reference wallets with origin_kind per row — officer exhibit, opens in Excel" style={{ fontSize: 11 }}>⬇ Export CSV</button>
        </div>
      </div>

      {scan && (
        <div className="dp-table-wrap" style={{ marginBottom: 14, borderStyle: scan.running ? 'solid' : 'dashed', opacity: scan.running ? 1 : 0.95 }}>
          <div style={{ padding: '8px 12px', display: 'flex', flexWrap: 'wrap', gap: 12, alignItems: 'center', fontSize: 11 }}>
            <span><b>Full-bucket wallet scan</b> · source <span className="mono">{scan.gcs_bucket}/{scan.gcs_prefix}</span></span>
            <span className={scan.running ? 'ex-badge ex-badge-new' : 'ex-badge'}>{scan.running ? 'RUNNING' : 'idle'}</span>
            <span>scanned <b>{scan.scanned.toLocaleString()}</b> / {scan.limit.toLocaleString()} · found <b>{scan.found}</b> · {scan.rate_per_sec}/s</span>
            <span className="small dim">pending full-scan {scan.pending_full_scan.toLocaleString()} · pending live {scan.pending_live.toLocaleString()}</span>
            {scan.last_key && <span className="small dim mono" style={{ maxWidth: 320, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }} title={scan.last_key}>{scan.last_key}</span>}
            {scan.started_utc && <span className="small dim">since {scan.started_utc}</span>}
          </div>
        </div>
      )}

      <div className="st-cards" style={{ marginBottom: 14 }}>
        <div className="st-card st-card-accent">
          <div className="st-card-label">Wallet hits (bucket-decoded)</div>
          <div className="st-card-value accent">{(data?.wallet_hits_total ?? rows.length).toLocaleString()}</div>
          <div className="st-card-sub">distinct addresses {data?.distinct_wallets ?? 0} · every hit = live bucket object</div>
        </div>
        <div className="st-card">
          <div className="st-card-label">Distinct addresses</div>
          <div className="st-card-value">{(data?.distinct_wallets ?? 0).toLocaleString()}</div>
          <div className="st-card-sub">each with exact source file + view proof</div>
        </div>
        <div className="st-card">
          <div className="st-card-label">GCS Enrichment (live source)</div>
          <div className="st-card-value small">{data?.extraction_status ? `${(data.extraction_status.enriched ?? 0).toLocaleString()} / ${(data.extraction_status.objects ?? 0).toLocaleString()}` : '—'}</div>
          <div className="st-card-sub">pending full-scan {data?.extraction_status?.pending_full_scan?.toLocaleString() ?? '—'} · scan covers all images, not just hints</div>
        </div>
      </div>

      {data && data.by_network.length > 0 && (
        <div style={{ marginBottom: 12 }} className="small dim">
          By network:{' '}
          {data.by_network.map((n) => (
            <span key={n.network} className="mono" style={{ marginRight: 12 }}>
              {n.network}: <b>{n.count.toLocaleString()}</b>
            </span>
          ))}
        </div>
      )}

      <div className="lf-controls">
        <div className="lf-controls-left">
          <button className={`ex-toggle ${distinct ? 'ex-toggle-on' : ''}`} onClick={() => setDistinct((d) => !d)} title="One row per unique address with hit count — the officer action list; reaches old single-hit wallets">
            ⇄ {distinct ? 'Unique wallets ✓' : 'Group: unique wallets'}
          </button>
          <button className={`ex-toggle ${!distinct ? 'ex-toggle-on' : ''}`} onClick={() => setDistinct(false)} title={`Every object×wallet hit row across all pages (${(data?.wallet_hits_total ?? 0).toLocaleString()} rows, ${PAGE_SIZE} per page)`}>All hits · {(data?.wallet_hits_total ?? 0).toLocaleString()}</button>
          <span className="small dim">All rows: bucket-decoded wallets — exact source file + view proof + Arkham + Tronscan.</span>
        </div>
        <span className="small dim">Live hits: QR/slip/chat extraction; nil right now is backlog — {data?.extraction_status?.pending?.toLocaleString() ?? '—'} pending enrich.</span>
      </div>
      {error && <div className="money-error">Could not load crypto wallets: {error} <button className="btn" style={{ marginLeft: 8 }} onClick={refresh}>Retry</button></div>}

      <div className="dp-table-wrap">
        <table className="dp-table">
          <thead>
              <tr>
                <th>Address</th>
                <th>Network</th>
                <th>Label</th>
                <th>Origin</th>
                <th>Detail</th>
              <th>Status</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r: any) => (
              <tr key={r.key + (r.address ?? '')}>
                <td className="mono" style={{ fontWeight: 700, wordBreak: 'break-all' }}>{r.address}{r.checksum === 'invalid' && <span title="On-chain checksum failed — OCR-degraded read, treat as unverified"> ⚠</span>}</td>
                <td>{r.network ?? '—'}</td>
                <td className="small">{r.standard ?? '—'}</td>
                <td className="small dim mono" style={{ maxWidth: 220, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }} title={r.key || ''}>
                  {originShort(r.key ?? '')}
                </td>
                <td className="small dim" style={{ maxWidth: 320, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }} title={r.role === 'sender' ? 'From · sender' : r.role === 'beneficiary' ? 'To · recipient' : ''}>
                  {fmtMoney(r.amount_value ?? r.amount_inr, r.amount_currency ?? (r.amount_inr != null ? 'INR' : null))}
                </td>
                <td>
                  <span className="ex-badge" title="Extraction confidence from the bucket image">{r.confidence || 'EXTRACTED'}{r.ocr_recovered ? ' · OCR' : ''}</span>
                  {(r as any).hits != null && <span className="ex-badge" title={`Seen in ${(r as any).hits} bucket object(s) · ${(r as any).first_seen ?? ''} → ${(r as any).last_seen ?? ''}`} style={{ marginLeft: 6 }}>{(r as any).hits}× hits</span>}
                </td>
                <td style={{ whiteSpace: 'nowrap' }}><a href={objectUrl(r.key)} target="_blank" rel="noreferrer" className="small">view proof</a>{' · '}{r.address && <><a href={arkhamAddressUrl(r.address)} target="_blank" rel="noreferrer" className="small">Arkham ↗</a>{' · '}<a href={bestTronscanUrl(r.address, r.txids) ?? '#'} target="_blank" rel="noreferrer" className="small">{tronscanLabel(r.txids)}</a></>}</td>
              </tr>
            ))}
            {rows.length === 0 && (
              <tr>
                <td colSpan={7} className="dp-empty">
                  <div className="dp-empty-icon">₿</div>
                  <div style={{ fontWeight: 700, marginBottom: 4 }}>No wallets yet on this monitor host</div>
                  <div className="small dim" style={{ maxWidth: 720, margin: '0 auto', lineHeight: 1.5 }}>
                    This monitor extracts wallets live from <span className="mono">bankingqr/</span>, <span className="mono">qrcode/</span>, <span className="mono">payment/</span>, <span className="mono">deposit/</span>, <span className="mono">withdrawal/</span> and crypto-named <span className="mono">chat_file/</span> screenshots. On this host enrichment is {data?.extraction_status ? `${data.extraction_status.enriched?.toLocaleString()} of ${data.extraction_status.objects?.toLocaleString()} (${data.extraction_status.pending?.toLocaleString()} pending)` : 'still draining'} — so nil is the backlog, not the data.
                  </div>
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>

      <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginTop: 12 }}>
        <button className="btn" disabled={page === 0} onClick={() => setPage((p) => Math.max(0, p - 1))}>← Prev</button>
        <button className="btn" disabled={page + 1 >= pageCount} onClick={() => setPage((p) => p + 1)}>Next →</button>
        <span className="small dim">Page {page + 1} of {pageCount} · showing {from.toLocaleString()}–{to.toLocaleString()} of {total.toLocaleString()} {distinct ? 'unique wallets' : 'hits'} (all pages reachable — old wallets included)</span>
      </div>

      <div className="pg-header" style={{ marginTop: 26 }}>
        <div className="pg-header-left">
          <span className="pg-header-icon">📋</span>
          <div>
            <h1 className="pg-header-title">Identified wallets (reference)</h1>
            <div className="pg-header-sub">
              All {(ref?.identified_total ?? 0).toLocaleString()} previously-identified wallets — hawala, platform-hot, operator, beneficiary, desk, dashboard rails — each with its original source + proof link. {(ref?.live_decoded ?? 0).toLocaleString()} also live-decoded above · {(ref?.missing_from_live ?? 0).toLocaleString()} reference-only (source was a spreadsheet/config or an object outside the bucket mirror — proof link is the evidence, not this tab).
            </div>
          </div>
        </div>
      </div>

      <div className="dp-table-wrap">
        <table className="dp-table">
          <thead>
            <tr>
              <th>Address</th>
              <th>Network</th>
              <th>Role</th>
              <th>Attribution</th>
              <th>Source</th>
              <th>Proof</th>
              <th>Live</th>
            </tr>
          </thead>
          <tbody>
            {(ref?.items ?? []).map((w) => (
              <tr key={w.address}>
                <td className="mono" style={{ fontWeight: 700, wordBreak: 'break-all' }}>{w.address}</td>
                <td>{w.network ?? '—'}</td>
                <td className="small">{w.role ?? '—'}</td>
                <td className="small">{w.attribution || '—'}</td>
                <td className="small dim mono" style={{ maxWidth: 220, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }} title={w.source_ref || ''}>
                  {w.source_ref || '—'}
                </td>
                <td style={{ whiteSpace: 'nowrap' }}>
                  {w.proof_url
                    ? <a href={w.proof_url} target="_blank" rel="noreferrer" className="small">S3 proof ↗</a>
                    : w.proof_s3_key
                      ? <a href={objectUrl(w.proof_s3_key)} target="_blank" rel="noreferrer" className="small">view proof</a>
                      : <span className="small dim">report §</span>}
                  {w.address && <>{' · '}<a href={arkhamAddressUrl(w.address)} target="_blank" rel="noreferrer" className="small">Arkham ↗</a>{' · '}<a href={bestTronscanUrl(w.address, null) ?? '#'} target="_blank" rel="noreferrer" className="small">Tronscan ↗</a></>}
                </td>
                <td>
                  {w.in_live
                    ? <span className="ex-badge" title="Also decoded from a live bucket image in the table above">live ✓</span>
                    : <span className="ex-badge ex-badge-new" title="Not decoded from any bucket image currently in the mirror — source object is a spreadsheet/config or outside the mirror">ref-only</span>}
                </td>
              </tr>
            ))}
            {(ref?.items ?? []).length === 0 && (
              <tr>
                <td colSpan={7} className="dp-empty">
                  <div className="dp-empty-icon">📋</div>
                  <div style={{ fontWeight: 700, marginBottom: 4 }}>Loading reference wallets…</div>
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
      <div className="small dim" style={{ marginTop: 8 }}>
        Attribution labels are report attributions, not proof of ownership/control — corroborate via Tronscan history, VASP/KYC, banking records, device evidence.
      </div>
    </>
  )
}
