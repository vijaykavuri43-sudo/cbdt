import { useEffect, useState } from 'react'
import { api, objectUrl, type ExtractPayload, type ObjRow } from '../lib/api'
import { arkhamAddressUrl, bestTronscanUrl, tronscanLabel } from '../lib/explorers'
import { extOf, fmtBytes, fmtTime, kindOf, shortKey } from '../lib/format'
import { SheetViewer } from './SheetViewer'

const SHEET_EXTS = new Set(['.csv', '.xlsx', '.xls'])

function isSheet(key: string) {
  return SHEET_EXTS.has(extOf(key))
}

export function Preview({ obj, onClose }: { obj: ObjRow; onClose: () => void }) {
  const [text, setText] = useState<string>('')
  const [enriching, setEnriching] = useState(false)
  const [geminiBusy, setGeminiBusy] = useState(false)
  const [geminiEnabled, setGeminiEnabled] = useState<boolean | null>(null)
  const [zoom, setZoom] = useState(1)
  const [copied, setCopied] = useState('')
  const kind = kindOf(obj.key)

  useEffect(() => {
    api<any>('/api/status').then((s) => setGeminiEnabled(!!s.gemini_enabled)).catch(()=>{})
  }, [])

  useEffect(() => {
    const h = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose()
      else if (e.key === '+' || e.key === '=') setZoom((z) => Math.min(4, z * 1.25))
      else if (e.key === '-' || e.key === '_') setZoom((z) => Math.max(0.4, z / 1.25))
      else if (e.key === '0') setZoom(1)
    }
    window.addEventListener('keydown', h)
    return () => window.removeEventListener('keydown', h)
  }, [onClose])

  useEffect(() => {
    if (kind !== 'text') return
    let alive = true
    fetch(objectUrl(obj.key))
      .then((r) => r.text())
      .then((t) => {
        if (!alive) return
        if (extOf(obj.key) === '.json') {
          try {
            t = JSON.stringify(JSON.parse(t), null, 2)
          } catch {}
        }
        setText(t.slice(0, 20000))
      })
    return () => {
      alive = false
    }
  }, [obj.key, kind])

  async function rerunOcr(withGemini = false) {
    if (withGemini) setGeminiBusy(true); else setEnriching(true)
    try {
      await postEnrich(obj.key, withGemini)
      window.dispatchEvent(new CustomEvent('obj-updated', { detail: obj.key }))
    } finally {
      setEnriching(false); setGeminiBusy(false)
    }
  }

  function copy(v: string, label: string) {
    navigator.clipboard.writeText(v)
    setCopied(label)
    setTimeout(() => setCopied(''), 1600)
  }

  const metaPairs: [string, string][] = [
    ['Key', obj.key],
    ['Size', fmtBytes(obj.size)],
    ['Modified', fmtTime(obj.last_modified)],
    ['Kind', kind],
  ]

  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div className="modal modal-wide" onClick={(e) => e.stopPropagation()}>
        <div className="modal-head">
          <div style={{ minWidth: 0 }}>
            <div className="mono small" style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
              <span className="mono small dim" style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                {shortKey(obj.key, 90)}
              </span>
              <button className="btn" style={{ padding: '2px 8px', fontSize: 11 }} onClick={() => copy(obj.key, 'key')}>
                {copied === 'key' ? '✓ Copied' : 'Copy key'}
              </button>
            </div>
            <div className="small dim">
              {fmtBytes(obj.size)} · modified {fmtTime(obj.last_modified)} · {kind}
            </div>
          </div>
          <div style={{ display: 'flex', gap: 8, alignItems: 'center', flexShrink: 0 }}>
            {kind === 'image' && (
              <div className="toolbar" style={{ margin: 0, gap: 4 }}>
                <button className="btn" onClick={() => setZoom((z) => Math.max(0.4, z / 1.25))} title="- (zoom out)">
                  −
                </button>
                <span className="mono small dim" style={{ minWidth: 42, textAlign: 'center' }}>
                  {Math.round(zoom * 100)}%
                </span>
                <button className="btn" onClick={() => setZoom((z) => Math.min(4, z * 1.25))} title="+ (zoom in)">
                  +
                </button>
                <button className="btn" onClick={() => setZoom(1)} title="0 reset">
                  1:1
                </button>
              </div>
            )}
            <a className="btn" href={objectUrl(obj.key, true)} download>
              Download
            </a>
            {(kind === 'image' || obj.extract === null) && (
              <>
                <button className="btn" disabled={enriching || geminiBusy} onClick={() => rerunOcr(false)}>
                  {enriching ? 'OCR…' : 'Re-run OCR'}
                </button>
                <button className="btn" disabled={enriching || geminiBusy || geminiEnabled === false} onClick={() => rerunOcr(true)} title={geminiEnabled === false ? 'Set GEMINI_API_KEY on server to enable' : 'Force Gemini Vision (principal amount only)'}>
                  {geminiBusy ? 'Gemini…' : 'Re-run with Gemini'}
                </button>
              </>
            )}
            <button className="btn btn-primary" onClick={onClose}>
              Close <span className="kbd" style={{ marginLeft: 4 }}>
                Esc
              </span>
            </button>
          </div>
        </div>

        {obj.extract && (
          <div className="extract-panel">
            <span className="badge">{obj.extract.kind === 'qr' ? 'QR DECODE' : obj.extract.kind === 'ocr' ? 'OCR' : obj.extract.kind?.toUpperCase()}</span>
            {obj.extract.amount_inr != null && (
              <span className="badge badge-money">₹ {obj.extract.amount_inr.toLocaleString('en-IN')}</span>
            )}
            {obj.extract.amount_inr == null && (obj.extract as any).amount_rejected_inr != null && (
              <span className="badge badge-warn">rejected ₹{(obj.extract as any).amount_rejected_inr.toLocaleString('en-IN')} (outside ₹1Cr sanity bound)</span>
            )}
            {(obj.extract as any).amount_tesseract != null && (
              <span className="badge" title="tesseract before Gemini correction">tesseract ₹{(obj.extract as any).amount_tesseract.toLocaleString('en-IN')} → gemini</span>
            )}
            {(obj.extract as any).amount_ignored && Array.isArray((obj.extract as any).amount_ignored) && (
              <span className="badge badge-warn" title={(obj.extract as any).amount_ignored.map((c:any)=>`${c.raw}=${c.value}`).join(', ')}>ignored {(obj.extract as any).amount_ignored.length} promo/balance amounts</span>
            )}
            {obj.extract.engine && <span className="badge" title="extraction engine">engine: {obj.extract.engine}</span>}
            {obj.extract.utr && <span className="badge">UTR {obj.extract.utr}</span>}
            {obj.extract.vpa && (
              <span className={'badge ' + (obj.extract.new_mule ? 'badge-new' : '')} style={{ display: 'inline-flex', gap: 6, alignItems: 'center' }}>
                VPA {obj.extract.vpa}
                <button className="btn" style={{ padding: '1px 6px', fontSize: 10 }} onClick={() => copy(obj.extract!.vpa!, 'vpa')}>
                  {copied === 'vpa' ? '✓' : 'Copy'}
                </button>
                {obj.extract.new_mule ? '(NEW)' : ''}
              </span>
            )}
            {obj.extract.payee_name && <span className="badge">{obj.extract.payee_name}</span>}
            {(obj.extract.wallets ?? []).map((w: any) => {
              const addr = String(w?.address ?? '').trim()
              if (!addr) return null
              const txids = (obj.extract as any)?.txids
              const tron = bestTronscanUrl(addr, txids)
              return (
                <span key={addr} className="badge" style={{ display: 'inline-flex', gap: 6, alignItems: 'center' }}>
                  ₿ {addr.slice(0, 20)}{addr.length > 20 ? '…' : ''} ({w.network ?? '?'})
                  <button className="btn" style={{ padding: '1px 6px', fontSize: 10 }} onClick={() => copy(addr, `w-${addr}`)}>
                    {copied === `w-${addr}` ? '✓' : 'Copy'}
                  </button>
                  <a href={arkhamAddressUrl(addr)} target="_blank" rel="noreferrer" style={{ fontSize: 10 }}>Arkham ↗</a>
                  {tron && <a href={tron} target="_blank" rel="noreferrer" style={{ fontSize: 10 }}>{tronscanLabel(txids)}</a>}
                </span>
              )
            })}
            {(obj.extract as any).beneficiary_name && <span className="badge">beneficiary {(obj.extract as any).beneficiary_name}</span>}
            {obj.extract.confidence && <span className="badge badge-warn">confidence: {obj.extract.confidence}</span>}
            {(obj.extract as any)._gemini_error && <span className="badge badge-warn">gemini error: {(obj.extract as any)._gemini_error}</span>}
            {(obj.extract.text_sample || obj.extract.raw_text) && (
              <div className="extract-text mono">{obj.extract.text_sample || obj.extract.raw_text}</div>
            )}
          </div>
        )}

        <div className="modal-body" style={{ display: 'flex', gap: 16, alignItems: 'flex-start', flexWrap: 'wrap' }}>
          <div style={{ flex: '1 1 520px', minWidth: 320, overflow: 'auto', maxHeight: '68vh', display: 'flex', justifyContent: 'center' }}>
            {isSheet(obj.key) ? (
              <div style={{ width: '100%' }}>
                <SheetViewer obj={obj} />
              </div>
            ) : kind === 'image' ? (
              <img
                src={objectUrl(obj.key)}
                alt={obj.key}
                style={{
                  maxWidth: '100%',
                  maxHeight: zoom === 1 ? '68vh' : undefined,
                  transform: `scale(${zoom})`,
                  transformOrigin: 'center top',
                  display: 'block',
                  transition: 'transform .14s',
                }}
              />
            ) : kind === 'audio' ? (
              <audio controls autoPlay src={objectUrl(obj.key)} style={{ width: '100%' }} />
            ) : kind === 'video' ? (
              <video controls src={objectUrl(obj.key)} style={{ maxWidth: '100%', maxHeight: '68vh' }} />
            ) : kind === 'text' ? (
              <pre className="preview-text" style={{ width: '100%' }}>
                {text}
              </pre>
            ) : kind === 'other' ? (
              <p className="dim">
                No inline preview for this type. Use Download to fetch it with sha256 evidence logging.
              </p>
            ) : null}
          </div>
          <div style={{ flex: '0 0 260px', minWidth: 220, display: 'flex', flexDirection: 'column', gap: 12 }}>
            <div className="panel" style={{ margin: 0, padding: 12 }}>
              <h3 style={{ marginBottom: 8 }}>Evidence</h3>
              {metaPairs.map(([k, v]) => (
                <div key={k} style={{ display: 'flex', justifyContent: 'space-between', gap: 10, padding: '5px 0', borderBottom: '1px solid rgba(255,255,255,.06)' }}>
                  <span className="tiny dim" style={{ textTransform: 'uppercase', letterSpacing: .6, fontWeight: 600 }}>
                    {k}
                  </span>
                  <span className="tiny mono" style={{ textAlign: 'right', wordBreak: 'break-all', maxWidth: 150 }}>
                    {v}
                  </span>
                </div>
              ))}
              <button className="btn" style={{ width: '100%', marginTop: 10 }} onClick={() => copy(obj.key, 'key2')}>
                {copied === 'key2' ? '✓ Key copied' : 'Copy original object key'}
              </button>
            </div>
            {kind === 'image' && (
              <div className="small dim" style={{ lineHeight: 1.6 }}>
                Tip: <span className="kbd">+</span> / <span className="kbd">−</span> to zoom, drag image, <span className="kbd">Esc</span> to close.
              </div>
            )}
          </div>
        </div>
      </div>
    </div>
  )
}

export function ExtractPanel({ x }: { x: ExtractPayload }) {
  if (!x.kind) return null
  const label =
    x.kind === 'qr' ? 'QR DECODE' : x.kind === 'ocr' ? 'OCR' : x.kind.toUpperCase()
  return (
    <div className="extract-panel">
      <span className="badge">{label}</span>
      {x.amount_inr != null && (
        <span className="badge badge-money">₹ {x.amount_inr.toLocaleString('en-IN')}</span>
      )}
      {x.utr && <span className="badge">UTR {x.utr}</span>}
      {x.vpa && (
        <span className={'badge ' + (x.new_mule ? 'badge-new' : '')}>
          VPA {x.vpa} {x.new_mule ? '(NEW — not in master list)' : ''}
        </span>
      )}
      {x.payee_name && <span className="badge">{x.payee_name}</span>}
      {x.confidence && <span className="badge badge-warn">confidence: {x.confidence}</span>}
      {(x.text_sample || x.raw_text) && (
        <div className="extract-text mono">{x.text_sample || x.raw_text}</div>
      )}
    </div>
  )
}

async function postEnrich(key: string, withGemini = false) {
  const params = new URLSearchParams({ key })
  if (withGemini) params.set('gemini', '1')
  // preserve auth token from api helper logic (post uses same header path, but we mimic token via helper)
  const tok = localStorage.getItem('s3lm_token')
  if (tok) params.set('token', tok)
  const r = await fetch(`/api/enrich?${params.toString()}`, { method: 'POST' })
  if (!r.ok) throw new Error(await r.text())
  return r.json()
}

export function useObjUpdated(refresh: () => void) {
  useEffect(() => {
    const h = () => refresh()
    window.addEventListener('obj-updated', h)
    return () => window.removeEventListener('obj-updated', h)
  }, [refresh])
}

export async function ensureRow(row: any): Promise<ObjRow> {
  if ('extract' in row) return row as ObjRow
  return api<ObjRow>(`/api/list`, {}).then(() => row as ObjRow)
}
