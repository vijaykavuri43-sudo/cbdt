import { useCallback, useEffect, useState } from 'react'
import { api, eventsUrl, exportWalletsUrl, objectUrl, post, tabHidden } from '../lib/api'
import { arkhamAddressUrl, bestTronscanUrl, tronscanLabel } from '../lib/explorers'
import { fmtMoney, fmtTime } from '../lib/format'

interface WalletRow {
  key: string
  last_modified: string
  address?: string
  network?: string
  standard?: string
  confidence?: string
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

  const items = (data?.items ?? []).filter((i) => (onlyNew ? !i.known_operator : true))

  function exportTsv() {
    const header = 'Address\tNetwork\tStandard\tAmount\tCurrency\tTxid\tArkham\tTronscan\tStatus\tSource File\tUploaded\n'
    const rows = items
      .map((r) => {
        const tx = Array.isArray(r.txids) ? r.txids.join(',') : (r.txids ?? '')
        const tron = bestTronscanUrl(r.address, r.txids) ?? ''
        const arkham = r.address ? arkhamAddressUrl(r.address) : ''
        return [
          r.address,
          r.network ?? '',
          r.standard ?? '',
          r.amount_value ?? r.amount_inr ?? '',
          r.amount_currency ?? (r.amount_inr != null ? 'INR' : ''),
          tx,
          arkham,
          tron,
          r.known_operator ? 'known-operator' : 'NEW',
          r.key,
          r.last_modified,
        ].join('\t')
      })
      .join('\n')
    const blob = new Blob([header + rows], { type: 'text/tab-separated-values' })
    const a = document.createElement('a')
    a.href = URL.createObjectURL(blob)
    a.download = `crypto-wallets-${new Date().toISOString().slice(0, 10)}.tsv`
    a.click()
  }
  function exportMasterTsv() {
    if (!data?.master_rows?.length) return
    const header = 'Entity\tNetwork\tAddress\tArkham\tTronscan\tLabel\tCase\tConfidence\tFact type\tNotes\tSource CSV\n'
    const rows = data.master_rows
      .map((r) =>
        [r.entity_id, r.network, r.address, arkhamAddressUrl(r.address), bestTronscanUrl(r.address) ?? '', r.label, r.case_ids, r.confidence, r.fact_type, '"' + r.notes.replace(/"/g, '""') + '"', data.master_source].join('\t')
      )
      .join('\n')
    const blob = new Blob([header + rows], { type: 'text/tab-separated-values' })
    const a = document.createElement('a')
    a.href = URL.createObjectURL(blob)
    a.download = `master-operator-wallets-${new Date().toISOString().slice(0, 10)}.tsv`
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
              {(data?.master_size ?? 0).toLocaleString()} known operator · master only for badge: <span className="mono">{data?.master_source ?? 'LE-SUBMISSION-MASTER/17-CRYPTO-WALLETS.csv'}</span>
            </div>
          </div>
        </div>
        <div className="pg-header-right">
          <button className="btn" onClick={refresh} style={{ fontSize: 11, marginRight: 8 }}>↻ Refresh</button>
          <button className="btn" onClick={startFullScan} style={{ fontSize: 11, marginRight: 8, borderColor: scan?.running ? 'var(--accent)' : undefined, opacity: scan?.running ? 0.6 : 1 }} disabled={!!scan?.running}>{scan?.running ? `⏳ scanning ${scan.scanned.toLocaleString()}` : '◉ Full wallet scan (GCS)'}</button>
          <button className="btn" onClick={exportFullCsv} title="Full wallet list (live + master) with source file + dashboard link per row — officer exhibit, opens in Excel" style={{ fontSize: 11, marginRight: 8 }}>⬇ Export FULL CSV (LE)</button>
          <button className="btn" onClick={exportTsv} style={{ fontSize: 11, marginRight: 8 }}>⬇ Export live ({items.length})</button>
          <button className="btn" onClick={exportMasterTsv} style={{ fontSize: 11 }}>⬇ Export master ({data?.master_rows?.length ?? 0})</button>
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
          <div className="st-card-label">Live wallet hits (GCS source files)</div>
          <div className="st-card-value accent">{items.length.toLocaleString()}</div>
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
          <div className="st-card-sub">from {data?.master_source ?? '17-CRYPTO-WALLETS.csv'} · provenance above</div>
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

      {/* Always-visible master provenance — even when live hits nil due to backlog */}
      <div className="dp-table-wrap" style={{ marginBottom: 18, borderColor: 'var(--accent2)', borderWidth: 1 }}>
        <div style={{ padding: '10px 14px', display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 12, borderBottom: '1px solid var(--border)', background: 'rgba(46,184,126,0.06)' }}>
          <div>
            <div style={{ fontSize: 13, fontWeight: 800 }}>Identified operator wallets — LE Submission Master (sources)</div>
            <div className="small dim" style={{ marginTop: 2 }}>
              Authoritative TRC-20 list used for all cross-checks · source <span className="mono">{data?.master_source ?? 'LE-SUBMISSION-MASTER/17-CRYPTO-WALLETS.csv'}</span> · CASE-2026-102 · <span className="ex-badge" style={{ borderColor: 'var(--accent2)', color: 'var(--accent2)', fontSize: 9 }}>high · fact</span> · notes include screenshot provenance
            </div>
          </div>
          <span className="small dim">{data?.master_rows?.length ?? 0} rows</span>
        </div>
        <table className="dp-table">
          <thead>
            <tr>
              <th>Address (TRON)</th>
              <th>Network</th>
              <th>Label</th>
              <th>Case</th>
              <th>Notes / source of screenshot</th>
              <th>Fact</th>
              <th>Links</th>
            </tr>
          </thead>
          <tbody>
            {(data?.master_rows ?? []).map((r) => (
              <tr key={r.address}>
                <td className="mono" style={{ fontWeight: 600, wordBreak: 'break-all' }}>{r.address}</td>
                <td>{r.network}</td>
                <td>{r.label || '—'}</td>
                <td className="small mono">{r.case_ids || '—'}</td>
                <td className="small wrap" style={{ maxWidth: 420 }}>{r.notes || '—'}</td>
                <td><span className="ex-badge" style={{ fontSize: 10 }}>{r.confidence} · {r.fact_type}</span></td>
                <td className="small" style={{ whiteSpace: 'nowrap' }}><a href={arkhamAddressUrl(r.address)} target="_blank" rel="noreferrer">Arkham ↗</a>{' · '}<a href={bestTronscanUrl(r.address) ?? '#'} target="_blank" rel="noreferrer">Tronscan ↗</a></td>
              </tr>
            ))}
            {(!data || (data.master_rows?.length ?? 0) === 0) && (
              <tr><td colSpan={7} className="dp-empty small dim">No master rows — place 17-CRYPTO-WALLETS.csv in LE-SUBMISSION-MASTER/ or &lt;data-dir&gt;/reference/ on this host.</td></tr>
            )}
          </tbody>
        </table>
      </div>

      <div className="lf-controls">
        <div className="lf-controls-left">
          <button className={`ex-toggle ${!onlyNew ? 'ex-toggle-on' : ''}`} onClick={() => setOnlyNew(false)}>
            All live · {data?.items.length ?? 0}
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
              <th>Std</th>
              <th className="num">Amount</th>
              <th>Status</th>
              <th>Source (where found)</th>
              <th>Uploaded</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            {items.map((r) => (
              <tr key={r.key + (r.address ?? '')}>
                <td className="mono" style={{ fontWeight: r.known_operator ? 400 : 700 }}>{r.address}</td>
                <td>{r.network ?? '—'}</td>
                <td className="small dim">{r.standard ?? '—'}</td>
                <td className="num">{fmtMoney(r.amount_value ?? r.amount_inr, r.amount_currency ?? (r.amount_inr != null ? 'INR' : null))}</td>
                <td>
                  {r.known_operator ? (
                    <span className="ex-badge" style={{ borderColor: 'var(--accent2)', color: 'var(--accent2)' }}>known operator</span>
                  ) : (
                    <span className="ex-badge ex-badge-new">NEW</span>
                  )}
                </td>
                <td className="small dim mono" style={{ maxWidth: 260, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }} title={r.key}>{r.key}</td>
                <td className="small dim">{fmtTime(r.last_modified)}</td>
                <td><a href={objectUrl(r.key)} target="_blank" rel="noreferrer" className="small">view proof</a>{r.address && <>{' · '}<a href={arkhamAddressUrl(r.address)} target="_blank" rel="noreferrer" className="small">Arkham ↗</a>{' · '}<a href={bestTronscanUrl(r.address, r.txids) ?? '#'} target="_blank" rel="noreferrer" className="small">{tronscanLabel(r.txids)}</a></>}</td>
              </tr>
            ))}
            {items.length === 0 && (
              <tr>
                <td colSpan={8} className="dp-empty">
                  <div className="dp-empty-icon">₿</div>
                  <div style={{ fontWeight: 700, marginBottom: 4 }}>No live wallets extracted yet on this monitor host</div>
                  <div className="small dim" style={{ maxWidth: 720, margin: '0 auto', lineHeight: 1.5 }}>
                    This monitor extracts wallets live from <span className="mono">bankingqr/</span>, <span className="mono">qrcode/</span>, <span className="mono">payment/</span>, <span className="mono">deposit/</span>, <span className="mono">withdrawal/</span> and crypto-named <span className="mono">chat_file/</span> screenshots (trustapp/usdt/tron/wallet…). On this host enrichment is {data?.extraction_status ? `${data.extraction_status.enriched?.toLocaleString()} of ${data.extraction_status.objects?.toLocaleString()} (${data.extraction_status.pending?.toLocaleString()} pending)` : 'still draining'} — so nil is the backlog, not the data. The <b>identified operator wallets are above</b> (master) and are always cross-checked; live hits will appear here as the queue drains. Use the master Export above for freezes.
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
