import { useEffect, useState, type CSSProperties } from 'react'
import { api, objectUrl, CAN_PLAY_WEBM_OPUS, type ExtractPayload, type ObjRow } from '../lib/api'
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
  // Full-size media can 404 while the object is still landing in the mirror
  // (thumb cached earlier) — never show a bare broken-image icon.
  const [imgErr, setImgErr] = useState(false)
  const [imgRetry, setImgRetry] = useState(0)
  const [mediaErr, setMediaErr] = useState(false)
  const kind = kindOf(obj.key)

  useEffect(() => {
    setImgErr(false); setImgRetry(0); setMediaErr(false); setZoom(1)
  }, [obj.key])

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

  const isPdf = obj.key.toLowerCase().endsWith('.pdf')
  // Slip-first layout: image + extract side-by-side (slip left, facts right)
  // so officers read the slip without scrolling past badge clutter (2026-09-11).
  const sideBySide = kind === 'image' && !!obj.extract && !isSheet(obj.key)

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

        {!sideBySide && obj.extract && (
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
          <div style={{ flex: sideBySide ? '1 1 560px' : '1 1 520px', minWidth: 320, overflow: 'auto', maxHeight: '70vh', display: 'flex', justifyContent: 'center' }}>
            {isSheet(obj.key) ? (
              <div style={{ width: '100%' }}>
                <SheetViewer obj={obj} />
              </div>
            ) : kind === 'image' ? (
              !obj.size ? (
                <div style={{ textAlign: 'center', padding: '48px 24px', maxWidth: 420 }}>
                  <div style={{ fontSize: 28, marginBottom: 8 }}>📁</div>
                  <div className="small" style={{ marginBottom: 4 }}>Empty object (0 bytes)</div>
                  <div className="tiny dim" style={{ marginBottom: 16, lineHeight: 1.6 }}>
                    This is a folder marker, not evidence — it has no image content to preview.
                  </div>
                </div>
              ) : imgErr ? (
                <div style={{ textAlign: 'center', padding: '48px 24px', maxWidth: 420 }}>
                  <div style={{ fontSize: 28, marginBottom: 8 }}>🖼️</div>
                  <div className="small" style={{ marginBottom: 4 }}>Couldn't load the full image</div>
                  <div className="tiny dim" style={{ marginBottom: 16, lineHeight: 1.6 }}>
                    The file may still be landing in the evidence mirror (listings and thumbnails can appear first). Retry, or Download — downloads are sha256-verified.
                  </div>
                  <div style={{ display: 'flex', gap: 8, justifyContent: 'center' }}>
                    <button className="btn" onClick={() => { setImgErr(false); setImgRetry((r) => r + 1) }}>
                      ↻ Retry
                    </button>
                    <a className="btn btn-primary" href={objectUrl(obj.key, true)} download>
                      Download
                    </a>
                  </div>
                </div>
              ) : (
                <img
                  src={objectUrl(obj.key) + (imgRetry ? `&_r=${imgRetry}` : '')}
                  alt={obj.key}
                  onError={() => setImgErr(true)}
                  style={{
                    maxWidth: '100%',
                    maxHeight: zoom === 1 ? '68vh' : undefined,
                    transform: `scale(${zoom})`,
                    transformOrigin: 'center top',
                    display: 'block',
                    transition: 'transform .14s',
                  }}
                />
              )
            ) : kind === 'audio' ? (
              mediaErr ? (
                <div style={{ textAlign: 'center', padding: '32px 16px', width: '100%' }}>
                  <div className="small" style={{ marginBottom: 12 }}>browser can't preview this clip</div>
                  <a className="btn btn-primary" href={objectUrl(obj.key, true)} download>⛨ Download</a>
                </div>
              ) : (
                <audio controls preload="metadata" src={CAN_PLAY_WEBM_OPUS ? objectUrl(obj.key) : objectUrl(obj.key, false, 0, 'wav')} style={{ width: '100%' }} onError={() => setMediaErr(true)} />
              )
            ) : kind === 'video' ? (
              mediaErr ? (
                <div style={{ textAlign: 'center', padding: '32px 16px', width: '100%' }}>
                  <div className="small" style={{ marginBottom: 12 }}>browser can't preview this video</div>
                  <a className="btn btn-primary" href={objectUrl(obj.key, true)} download>⛨ Download</a>
                </div>
              ) : (
                <video controls preload="metadata" src={objectUrl(obj.key)} style={{ maxWidth: '100%', maxHeight: '68vh' }} onError={() => setMediaErr(true)} />
              )
            ) : kind === 'text' ? (
              <pre className="preview-text" style={{ width: '100%' }}>
                {text}
              </pre>
            ) : kind === 'other' ? (
              isPdf ? (
                <iframe title="pdf preview" src={objectUrl(obj.key)} style={{ width: '100%', height: '68vh', border: '1px solid rgba(255,255,255,.12)', borderRadius: 8 }} />
              ) : (
                <p className="dim">
                  No inline preview for this type. Use Download to fetch it with sha256 evidence logging.
                </p>
              )
            ) : null}
          </div>
          <div style={{ flex: '0 0 260px', minWidth: 220, display: 'flex', flexDirection: 'column', gap: 12 }}>
            {sideBySide && obj.extract ? (
              <ExtractFacts x={obj.extract} objKey={obj.key} copied={copied} onCopy={copy} />
            ) : (
              kind === 'image' && (
                <div className="small dim" style={{ lineHeight: 1.6 }}>
                  Tip: <span className="kbd">+</span> / <span className="kbd">−</span> to zoom, drag image, <span className="kbd">Esc</span> to close.
                </div>
              )
            )}
          </div>
        </div>
      </div>
    </div>
  )
}

/** Right-hand facts column for the slip-first popup layout: one compact row
 * per field, copy buttons inline, raw OCR text collapsed (no scroll clutter). */
function ExtractFacts({ x, objKey, copied, onCopy }: {
  x: ExtractPayload
  objKey: string
  copied: string
  onCopy: (v: string, label: string) => void
}) {
  const anyX = x as any
  const row: CSSProperties = {
    display: 'flex', justifyContent: 'space-between', alignItems: 'center',
    gap: 8, padding: '7px 10px', borderRadius: 8,
    background: 'rgba(255,255,255,.035)', border: '1px solid rgba(255,255,255,.07)',
  }
  const label: CSSProperties = {
    fontSize: 10, letterSpacing: '.08em', color: 'var(--dim, #8b93a7)',
    textTransform: 'uppercase', flexShrink: 0,
  }
  const val: CSSProperties = {
    fontSize: 13, textAlign: 'right', overflow: 'hidden',
    textOverflow: 'ellipsis', whiteSpace: 'nowrap', maxWidth: '100%',
  }
  const miniBtn: CSSProperties = { padding: '1px 7px', fontSize: 10, flexShrink: 0 }
  const twin = anyX.twin_artifact
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 8, minWidth: 0 }}>
      <div style={{ ...row, flexDirection: 'column', alignItems: 'stretch', gap: 2 }}>
        <span style={label}>Amount</span>
        {x.amount_inr != null ? (
          <span className="mono" style={{ fontSize: 24, fontWeight: 700, color: 'var(--money, #4ade80)' }}>
            ₹ {x.amount_inr.toLocaleString('en-IN')}
          </span>
        ) : (
          <span className="dim" style={{ fontSize: 14 }}>— not found <span className="tiny">(use Re-run OCR above)</span></span>
        )}
        {twin && (
          <span className="tiny" style={{ color: 'var(--warn, #fbbf24)' }}>
            auto-corrected: tesseract {Number(twin.artifact).toLocaleString('en-IN')} → {Number(twin.true_amount).toLocaleString('en-IN')} (₹-glyph misread)
          </span>
        )}
        {anyX.glyph_collision && (
          <span className="tiny" style={{ color: 'var(--warn, #fbbf24)' }}>glyph collision — verify against slip</span>
        )}
      </div>
      {x.vpa ? (
        <div style={row}>
          <span style={label}>VPA</span>
          <span className="mono" style={val} title={x.vpa}>{x.vpa}{x.new_mule ? ' (NEW)' : ''}</span>
          <button className="btn" style={miniBtn} onClick={() => onCopy(x.vpa!, 'vpa-f')}>{copied === 'vpa-f' ? '✓' : 'Copy'}</button>
        </div>
      ) : (
        <div style={row}><span style={label}>VPA</span><span className="dim">—</span></div>
      )}
      {x.utr && (
        <div style={row}>
          <span style={label}>UTR</span>
          <span className="mono" style={val} title={x.utr}>{x.utr}</span>
          <button className="btn" style={miniBtn} onClick={() => onCopy(x.utr!, 'utr-f')}>{copied === 'utr-f' ? '✓' : 'Copy'}</button>
        </div>
      )}
      {(x.payee_name || anyX.beneficiary_name) && (
        <div style={row}>
          <span style={label}>Payee</span>
          <span style={val} title={x.payee_name || anyX.beneficiary_name}>{x.payee_name || anyX.beneficiary_name}</span>
        </div>
      )}
      {(x.wallets ?? []).slice(0, 3).map((w: any) => {
        const addr = String(w?.address ?? '').trim()
        if (!addr) return null
        return (
          <div style={row} key={addr}>
            <span style={label}>{w.network ?? 'wallet'}</span>
            <span className="mono" style={val} title={addr}>{addr.slice(0, 18)}{addr.length > 18 ? '…' : ''}</span>
            <button className="btn" style={miniBtn} onClick={() => onCopy(addr, `w-f-${addr}`)}>{copied === `w-f-${addr}` ? '✓' : 'Copy'}</button>
          </div>
        )
      })}
      <div style={row}>
        <span style={label}>Engine</span>
        <span className="tiny dim" style={{ textAlign: 'right' }}>
          {x.kind === 'qr' ? 'QR decode' : x.engine || x.kind}{x.confidence ? ` · ${x.confidence}` : ''}
        </span>
      </div>
      {anyX.amount_rejected_inr != null && (
        <div className="tiny" style={{ color: 'var(--warn, #fbbf24)' }}>
          rejected ₹{Number(anyX.amount_rejected_inr).toLocaleString('en-IN')} (outside sanity bound)
        </div>
      )}
      {(x.text_sample || x.raw_text) && (
        <details className="tiny dim" style={{ ...row, display: 'block' }}>
          <summary style={{ cursor: 'pointer' }}>OCR text</summary>
          <div className="mono" style={{ whiteSpace: 'pre-wrap', wordBreak: 'break-word', marginTop: 6, maxHeight: 180, overflow: 'auto' }}>
            {x.text_sample || x.raw_text}
          </div>
        </details>
      )}
      <div className="tiny dim" style={{ lineHeight: 1.6 }}>
        <span className="kbd">+</span> / <span className="kbd">−</span> zoom · <span className="kbd">Esc</span> close · key: <span className="mono">{objKey.split('/').pop()}</span>
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
