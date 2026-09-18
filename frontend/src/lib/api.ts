const TOKEN_KEY = 's3lm_token'

export function getToken(): string {
  return localStorage.getItem(TOKEN_KEY) || ''
}

export function setToken(t: string) {
  localStorage.setItem(TOKEN_KEY, t.trim())
}

export function hasToken(): boolean {
  return !!getToken()
}

export class AuthError extends Error {}

// Tabs stay mounted when hidden — background intervals must not hammer the
// backend (or drain laptop/phone batteries). Gate every poller on this.
export function tabHidden(): boolean {
  try {
    return typeof document !== 'undefined' && document.hidden
  } catch {
    return false
  }
}

// Storm guard for live refresh triggers (2026-09-16: SSE 'enriched' /
// 'new_object' / 'wallet_found' events fire many times per second during
// intake; each unguarded trigger launched concurrent heavy aggregates that
// saturated the backend threadpool and exhausted browser sockets —
// net::ERR_INSUFFICIENT_RESOURCES. Interval path skips while a run is in
// flight; SSE nudges additionally respect a cooldown).
export function createRefreshGuard(cooldownMs = 20000) {
  let inflight = false
  let last = 0
  const settle = (v: unknown) => {
    if (v && typeof (v as Promise<unknown>).finally === 'function') (v as Promise<unknown>).finally(() => { inflight = false })
    else inflight = false
  }
  return {
    tryRun(fn: () => unknown) {
      if (inflight) return
      inflight = true
      last = Date.now()
      try {
        settle(fn())
      } catch {
        inflight = false
      }
    },
    nudge(fn: () => unknown) {
      if (inflight || Date.now() - last < cooldownMs) return
      inflight = true
      last = Date.now()
      try {
        settle(fn())
      } catch {
        inflight = false
      }
    },
  }
}

function authParams(): string {
  const t = getToken()
  return t ? `token=${encodeURIComponent(t)}` : ''
}

function authHeaders(): Record<string, string> {
  const t = getToken()
  return t ? { Authorization: `Bearer ${t}` } : {}
}

async function handle(r: Response): Promise<Response> {
  if (r.status === 401) {
    window.dispatchEvent(new CustomEvent('s3lm-unauth'))
    throw new AuthError('locked')
  }
  if (!r.ok) throw new Error(`${r.status} ${r.statusText}`)
  return r
}

export async function api<T = any>(path: string, params?: Record<string, any>, timeoutMs = 25000): Promise<T> {
  const p = path.startsWith('/api') ? path.slice(4) : path
  const qs = new URLSearchParams(clean(params ?? {}))
  const qstr = qs.toString()
  const url = '/api' + p + (qstr ? '?' + qstr : '')
  const ctrl = new AbortController()
  const timer = window.setTimeout(() => ctrl.abort(), timeoutMs)
  try {
    const r = await fetch(url, { headers: authHeaders(), signal: ctrl.signal })
    await handle(r)
    return r.json()
  } catch (e: any) {
    // A hung backend must NEVER leave the UI on skeleton forever — surface it.
    if (e?.name === 'AbortError') throw new Error(`request timed out after ${Math.round(timeoutMs / 1000)}s (${p || '/'} — backend busy? press Retry)`)
    throw e
  } finally {
    window.clearTimeout(timer)
  }
}

export async function post<T = any>(path: string, body?: any, timeoutMs = 120000): Promise<T> {
  const p = path.startsWith('/api') ? path.slice(4) : path
  const ctrl = new AbortController()
  const timer = window.setTimeout(() => ctrl.abort(), timeoutMs)
  try {
    const r = await fetch('/api' + p, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', ...authHeaders() },
      body: body === undefined ? undefined : JSON.stringify(body),
      signal: ctrl.signal,
    })
    await handle(r)
    return r.status === 204 ? ({} as T) : r.json()
  } catch (e: any) {
    if (e?.name === 'AbortError') throw new Error(`request timed out after ${Math.round(timeoutMs / 1000)}s (${p || '/'} — press Retry)`)
    throw e
  } finally {
    window.clearTimeout(timer)
  }
}

function clean(p: Record<string, any>) {
  const o: Record<string, any> = {}
  for (const [k, v] of Object.entries(p)) if (v !== undefined && v !== null && v !== '') o[k] = v
  return o
}

export function objectUrl(key: string, download = false, thumb = 0, format = ''): string {
  const at = authParams()
  return `/api/object?key=${encodeURIComponent(key)}${download ? '&download=1' : ''}${thumb ? `&thumb=${thumb}` : ''}${format ? `&format=${format}` : ''}${at ? '&' + at : ''}`
}

// True when this browser can play Opus-in-WebM voice notes natively
// (Chrome/Edge/Firefox). Safari answers '' and needs the ?format=wav
// server transcode instead of showing 'browser can't preview this clip'.
export const CAN_PLAY_WEBM_OPUS: boolean =
  typeof Audio !== 'undefined' && new Audio().canPlayType('audio/webm; codecs="opus"') !== ''

function _canPlay(mime: string): boolean {
  try {
    if (typeof Audio === 'undefined') return false
    return new Audio().canPlayType(mime) !== ''
  } catch {
    return false
  }
}

// Per-extension native-playability probe. AMR and 3GP have effectively no
// native browser support, so they ALWAYS go through ?format=wav (ffmpeg on
// the server) instead of rendering a dead 0:00 player. WebM/Opus/Ogg/M4A
// are probed live so capable browsers keep the zero-latency native path.
export function canPlayExtNative(ext: string): boolean {
  const e = ext.toLowerCase()
  if (e === '.amr' || e === '.3gp' || e === '.3g2') return false
  if (e === '.wav') return _canPlay('audio/wav')
  if (e === '.mp3') return _canPlay('audio/mpeg')
  if (e === '.webm' || e === '.opus') return CAN_PLAY_WEBM_OPUS
  if (e === '.ogg' || e === '.oga') return _canPlay('audio/ogg; codecs="opus"') || _canPlay('audio/ogg')
  if (e === '.m4a' || e === '.mp4') return _canPlay('audio/mp4') || _canPlay('audio/x-m4a')
  if (e === '.flac') return _canPlay('audio/flac')
  return true
}

// Voice-note URL: ALWAYS via ?format=wav (reliability fix 2026-09-16).
//
// The bucket holds many misnamed containers (.wav-that-is-WebM, .mp3-that-is
// octet-stream, ...). Serving those natively stalls the <audio> element at
// 0:00 with no onError on several browsers, so per-extension native probing
// cannot be trusted. The server only transcodes when sniffed bytes need it
// (_NEEDS_WAV_TRANSCODE) and serves genuine WAV/MP3/OGG/MP4/M4A/FLAC as-is,
// so this costs nothing for honest files and guarantees a playable clip.
// Transcodes are cached per-format server-side (once per clip).
export function voiceSrc(key: string): string {
  const base = key.split('/').pop() || ''
  const i = base.lastIndexOf('.')
  const ext = i >= 0 ? base.slice(i).toLowerCase() : ''
  if (ext) return objectUrl(key, false, 0, 'wav')
  return objectUrl(key)
}

export function eventsUrl(): string {
  const at = authParams()
  return `/api/events${at ? '?' + at : ''}`
}

export function exportRailsUrl(): string {
  const at = authParams()
  return `/api/export/rails${at ? '?' + at : ''}`
}

function exportUrl(kind: string): string {
  const at = authParams()
  return `/api/export/${kind}${at ? '?' + at : ''}`
}

export function exportUpisUrl(): string { return exportUrl('upis') }
export function exportWalletsUrl(): string { return exportUrl('wallets') }
export function exportPayoutsUrl(): string { return exportUrl('payout-accounts') }

/** Officer Query tab — filtered money-flows CSV (opens in Excel). */
export function exportMoneyFlowsUrl(params: Record<string, string | undefined>): string {
  const at = authParams()
  const qs = new URLSearchParams(clean(params))
  if (at) {
    const tok = new URLSearchParams(at)
    tok.forEach((v, k) => qs.set(k, v))
  }
  const qstr = qs.toString()
  return `/api/export/money-flows${qstr ? '?' + qstr : ''}`
}

export function exportMoneyFlowsUniqueUrl(params: Record<string, string | undefined>): string {
  const at = authParams()
  const qs = new URLSearchParams(clean(params))
  if (at) {
    const tok = new URLSearchParams(at)
    tok.forEach((v, k) => qs.set(k, v))
  }
  const qstr = qs.toString()
  return `/api/export/money-flows-unique${qstr ? '?' + qstr : ''}`
}

/** Fetch an export URL with the officer token and save it as a .csv file.
 *  A plain <a href> often opens CSV as a browser tab instead of downloading. */
export async function saveCsvFromUrl(url: string, filename: string, timeoutMs = 180000): Promise<void> {
  const ctrl = new AbortController()
  const timer = window.setTimeout(() => ctrl.abort(), timeoutMs)
  try {
    const r = await fetch(url, { headers: authHeaders(), signal: ctrl.signal })
    await handle(r)
    const blob = await r.blob()
    const file = blob.type && blob.type.includes('csv')
      ? blob
      : new Blob([blob], { type: 'text/csv;charset=utf-8' })
    const href = URL.createObjectURL(file)
    const a = document.createElement('a')
    a.href = href
    a.download = filename.endsWith('.csv') ? filename : `${filename}.csv`
    a.rel = 'noopener'
    document.body.appendChild(a)
    a.click()
    a.remove()
    window.setTimeout(() => URL.revokeObjectURL(href), 4000)
  } catch (e: any) {
    if (e?.name === 'AbortError') throw new Error(`CSV export timed out after ${Math.round(timeoutMs / 1000)}s`)
    throw e
  } finally {
    window.clearTimeout(timer)
  }
}

export interface Status {
  phase: string
  sweep_done: boolean
  sweep_progress: Record<string, { count?: number; error?: string }>
  objects_total: number
  new_since_start: number
  last_poll_utc: string | null
  bucket_live: boolean | null
  bucket_live_fails?: number
  bucket_live_ok_utc?: string | null
  started_utc: string
  bucket_url: string
  object_source?: string
  effective_source?: string
  case: string
  auth_ref: string
  prefixes: string[]
  enrichment: {
    total: number
    enriched_done: number
    with_payload: number
    pending_now: number
    slip_backlog?: number
    eligible_total?: number
    coverage_pct?: number
    processed_last_hour?: number
    eta_hours?: number | null
    errors?: number
  }
  gemini_enabled?: boolean
  gemini_model?: string | null
  max_amount_inr?: number
}

export interface ObjRow {
  key: string
  size: number | null
  last_modified: string
  seen_utc?: string
  etag?: string
  enriched?: number
  extract?: ExtractPayload | null
}

export interface ExtractPayload {
  kind?: string
  vpa?: string
  vpas?: string[]
  gambler?: string
  beneficiary_name?: string
  sender_name?: string
  bank_accounts?: Array<{ account_number?: string; masked_account?: string; masked?: boolean; holder?: string; ifsc?: string }>
  wallets?: Array<{ address: string; network?: string; standard?: string; confidence?: string }>
  ifsc?: string
  payee_name?: string
  amount_inr?: number | null
  utr?: string | null
  amount_rejected_inr?: number | null
  amount_tesseract?: number | null
  amount_ignored?: Array<{ raw: string; value: number; score: number; rejected: boolean; context: string }>
  amount_candidates?: Array<{ raw: string; value: number; score: number; rejected: boolean; context: string }>
  engine?: string
  known_mule?: boolean
  new_mule?: boolean
  confidence?: string
  text_sample?: string | null
  raw_text?: string | null
  _gemini?: any
  _gemini_error?: string
}
