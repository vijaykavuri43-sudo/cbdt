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

export function objectUrl(key: string, download = false, thumb = 0): string {
  const at = authParams()
  return `/api/object?key=${encodeURIComponent(key)}${download ? '&download=1' : ''}${thumb ? `&thumb=${thumb}` : ''}${at ? '&' + at : ''}`
}

export function eventsUrl(): string {
  const at = authParams()
  return `/api/events${at ? '?' + at : ''}`
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
  size: number
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
