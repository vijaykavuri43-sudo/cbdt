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
  const [data, setData] = useState<WalletsResp | null>(null)
  const [scan, setScan] = useState<FullScanStatus | null>(null)

  const refresh = useCallback(() => {
    api<WalletsResp>('/api/wallets', { limit: 1000 }).then(setData).catch(() => {})
  }, [])
  const refreshScan = useCallback(() => {
    api<FullScanStatus>('/api/wallets/full-scan/status').then(setScan).catch(() => {})
  }, [])
  useEffect(refresh, [refresh])
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

  function exportMergedCsv() {
    // One officer download: every row in the table below with exact source.
    const q = (v: any) => '"' + String(v ?? '').replace(/"/g, '""') + '"'
    const header = 'Address,Network,Label,Origin,Detail,Status,Arkham,Tronscan\n'
    const body = rows
      .map((r: any) => {
        return [
          q(r.address), q(r.network), q(r.standard || ''),
          q(r.key || ''), q(r.amount_value ?? r.amount_inr ?? ''),
          q(r.confidence || 'EXTRACTED'),
          q(r.address ? arkhamAddressUrl(r.address) : ''),
          q(bestTronscanUrl(r.address, r.txids) ?? ''),
        ].join(',')
      })
      .join('\n')
    const blob = new Blob([header + body], { type: 'text/csv' })
    const a = document.createElement('a')
    a.href = URL.createObjectURL(blob)
    a.download = `crypto-wallets-${new Date().toISOString().slice(0, 10)}.csv`
    a.click()
  }
  function exportFullCsv() {
    // Full officer exhibit: every wallet with exact source file + dashboard
    // link per row — server streams CSV (Excel-ready)
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
          <button className="btn" onClick={exportFullCsv} title="Full wallet list with exact source file + dashboard link per row — officer exhibit, opens in Excel" style={{ fontSize: 11, marginRight: 8 }}>⬇ Export FULL CSV</button>
          <button className="btn" onClick={exportMergedCsv} title="Every row in the table below as one CSV — opens in Excel" style={{ fontSize: 11 }}>⬇ Export table CSV ({rows.length})</button>
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
          <span className="small dim">All rows: bucket-decoded wallets — exact source file + view proof + Arkham + Tronscan.</span>
        </div>
        <span className="small dim">Live hits: QR/slip/chat extraction; nil right now is backlog — {data?.extraction_status?.pending?.toLocaleString() ?? '—'} pending enrich.</span>
      </div>

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
                  {(r.key ?? '').split('/').pop()}
                </td>
                <td className="small dim" style={{ maxWidth: 320, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }} title={r.role === 'sender' ? 'From · sender' : r.role === 'beneficiary' ? 'To · recipient' : ''}>
                  {fmtMoney(r.amount_value ?? r.amount_inr, r.amount_currency ?? (r.amount_inr != null ? 'INR' : null))}
                </td>
                <td>
                  <span className="ex-badge" title="Extraction confidence from the bucket image">{r.confidence || 'EXTRACTED'}{r.ocr_recovered ? ' · OCR' : ''}</span>
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
    </>
  )
}
