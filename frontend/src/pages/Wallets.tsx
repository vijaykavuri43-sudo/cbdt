import { useCallback, useEffect, useState } from 'react'
import { api, eventsUrl, exportWalletsUrl, objectUrl, post, tabHidden } from '../lib/api'
import { arkhamAddressUrl, bestTronscanUrl, tronscanLabel } from '../lib/explorers'
import { fmtMoney } from '../lib/format'

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
  known_operator?: boolean
  kind?: string | null
}
interface MasterWalletRow {
  entity_id: string
  network: string
  address: string
  address_lower: string
  label: string
  case_ids: string
  confidence: string
  fact_type: string
  notes: string
}
interface WalletsResp {
  master_size: number
  master_rows: MasterWalletRow[]
  master_source: string
  items: WalletRow[]
  new_not_in_master: number
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
  const [onlyNew, setOnlyNew] = useState(false)

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

  const liveItems = (data?.items ?? []).filter((i) => (onlyNew ? !i.known_operator : true))
  // Single merged table: authoritative master rows first (badged), then live hits.
  const merged = [
    ...((onlyNew ? [] : (data?.master_rows ?? [])).map((r: any) => ({
      _src: 'master' as const,
      key: 'master:' + r.address,
      address: r.address,
      network: r.network,
      label: r.label || '',
      caseIds: r.case_ids || '',
      detail: r.notes || '',
      status: `${r.confidence ?? ''} · ${r.fact_type ?? ''}`.replace(/^ · | · $/g, '') || 'master',
      txids: null as any,
      last_modified: '',
    }))),
    ...liveItems.map((r: any) => ({ ...r, _src: 'live' as const })),
  ]

  function exportMergedCsv() {
    // One officer download: every row in the merged table (master + live).
    const q = (v: any) => '"' + String(v ?? '').replace(/"/g, '""') + '"'
    const header = 'Address,Network,Label,Origin,Detail,Status,Case,Arkham,Tronscan\n'
    const rows = merged
      .map((r: any) => {
        const origin = r._src === 'master' ? (data?.master_source ?? 'LE-master') : r.key
        const detail = r._src === 'master' ? r.detail : (r.amount_value ?? r.amount_inr ?? '')
        const status = r._src === 'master' ? ('master · ' + r.status) : (r.known_operator ? 'known-operator' : 'NEW')
        return [
          q(r.address), q(r.network), q(r.label || r.standard || ''),
          q(origin), q(detail), q(status), q(r.caseIds || ''),
          q(r.address ? arkhamAddressUrl(r.address) : ''),
          q(bestTronscanUrl(r.address, r.txids) ?? ''),
        ].join(',')
      })
      .join('\n')
    const blob = new Blob([header + rows], { type: 'text/csv' })
    const a = document.createElement('a')
    a.href = URL.createObjectURL(blob)
    a.download = `crypto-wallets-merged-${new Date().toISOString().slice(0, 10)}.csv`
    a.click()
  }
  function exportFullCsv() {
    // Full officer exhibit: ALL live wallets + known-operator master with
    // source file + dashboard link per row — server streams CSV (Excel-ready)
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
              Live source: <span className="mono">{data?.source ?? 'gs://cbtf-case-evidence-518405669914/s3-mirror/'}</span> · TRON / EVM / BTC from QR · slips · all chat_file images (full-scan) · vs{' '}
              {(data?.master_size ?? 0).toLocaleString()} known operator (merged below) · master: <span className="mono">{data?.master_source ?? 'LE-SUBMISSION-MASTER/17-CRYPTO-WALLETS.csv'}</span>
            </div>
          </div>
        </div>
        <div className="pg-header-right">
          <button className="btn" onClick={refresh} style={{ fontSize: 11, marginRight: 8 }}>↻ Refresh</button>
          <button className="btn" onClick={startFullScan} style={{ fontSize: 11, marginRight: 8, borderColor: scan?.running ? 'var(--accent)' : undefined, opacity: scan?.running ? 0.6 : 1 }} disabled={!!scan?.running}>{scan?.running ? `⏳ scanning ${scan.scanned.toLocaleString()}` : '◉ Full wallet scan (GCS)'}</button>
          <button className="btn" onClick={exportFullCsv} title="Full wallet list (live + master) with source file + dashboard link per row — officer exhibit, opens in Excel" style={{ fontSize: 11, marginRight: 8 }}>⬇ Export FULL CSV (LE)</button>
          <button className="btn" onClick={exportMergedCsv} title="Every row in the table below (master + live) as one CSV — opens in Excel" style={{ fontSize: 11 }}>⬇ Export table CSV ({merged.length})</button>
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
          <div className="st-card-label">Wallets in table (master + live)</div>
          <div className="st-card-value accent">{merged.length.toLocaleString()}</div>
          <div className="st-card-sub">total hits {data?.wallet_hits_total ?? 0} · distinct {data?.distinct_wallets ?? 0} · every hit = live object</div>
        </div>
        <div className="st-card st-card-warn">
          <div className="st-card-label">NEW — not in master</div>
          <div className="st-card-value warn">{(data?.new_not_in_master ?? 0).toLocaleString()}</div>
          <div className="st-card-sub">master is only badge — source is GCS files</div>
        </div>
        <div className="st-card">
          <div className="st-card-label">Master (badge only)</div>
          <div className="st-card-value">{(data?.master_size ?? 0).toLocaleString()}</div>
          <div className="st-card-sub">from {data?.master_source ?? '17-CRYPTO-WALLETS.csv'} · merged in the table below</div>
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
          <button className={`ex-toggle ${!onlyNew ? 'ex-toggle-on' : ''}`} onClick={() => setOnlyNew(false)}>
            All · {merged.length}
          </button>
          <button className={`ex-toggle ${onlyNew ? 'ex-toggle-on' : ''}`} onClick={() => setOnlyNew(true)}>
            ⚠ NEW only · {data?.new_not_in_master ?? 0}
          </button>
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
            {merged.map((r: any) => (
              <tr key={r.key + (r.address ?? '')} style={r._src === 'master' ? { background: 'rgba(46,184,126,0.05)' } : undefined}>
                <td className="mono" style={{ fontWeight: r.known_operator || r._src === 'master' ? 400 : 700, wordBreak: 'break-all' }}>{r.address}{r.checksum === 'invalid' && <span title="On-chain checksum failed — OCR-degraded read, treat as unverified"> ⚠</span>}</td>
                <td>{r.network ?? '—'}</td>
                <td className="small">{r._src === 'master' ? (r.label || '—') : (r.standard ?? '—')}</td>
                <td className="small dim mono" style={{ maxWidth: 220, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }} title={r._src === 'master' ? (data?.master_source ?? 'LE-master') : r.key}>
                  {r._src === 'master' ? <span className="ex-badge" style={{ borderColor: 'var(--accent2)', color: 'var(--accent2)' }} title={data?.master_source ?? ''}>LE master</span> : (r.key ?? '').split('/').pop()}
                  {r._src === 'master' && r.caseIds ? <span className="small dim mono" style={{ marginLeft: 6 }}>{r.caseIds}</span> : null}
                </td>
                <td className="small dim" style={{ maxWidth: 320, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }} title={r._src === 'master' ? (r.detail || '') : (r.role === 'sender' ? 'From · sender' : r.role === 'beneficiary' ? 'To · recipient' : '')}>
                  {r._src === 'master' ? (r.detail || '—') : fmtMoney(r.amount_value ?? r.amount_inr, r.amount_currency ?? (r.amount_inr != null ? 'INR' : null))}
                </td>
                <td>
                  {r._src === 'master' ? (
                    <span className="ex-badge" style={{ borderColor: 'var(--accent2)', color: 'var(--accent2)' }} title="Authoritative LE submission list — used for all cross-checks">{r.status}</span>
                  ) : r.known_operator ? (
                    <span className="ex-badge" style={{ borderColor: 'var(--accent2)', color: 'var(--accent2)' }}>known operator</span>
                  ) : (
                    <span className="ex-badge ex-badge-new">NEW</span>
                  )}
                </td>
                <td style={{ whiteSpace: 'nowrap' }}>{r._src === 'live' && <><a href={objectUrl(r.key)} target="_blank" rel="noreferrer" className="small">view proof</a>{' · '}</>}{r.address && <><a href={arkhamAddressUrl(r.address)} target="_blank" rel="noreferrer" className="small">Arkham ↗</a>{' · '}<a href={bestTronscanUrl(r.address, r.txids) ?? '#'} target="_blank" rel="noreferrer" className="small">{tronscanLabel(r.txids)}</a></>}</td>
              </tr>
            ))}
            {merged.length === 0 && (
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
