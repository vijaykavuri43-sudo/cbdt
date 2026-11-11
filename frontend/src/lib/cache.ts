// Persistent stale-while-revalidate cache for dashboard GET responses.
//
// Why: every dashboard instance (:80/:81/:82/:83) is its own origin, so
// clicking a sibling bucket in the sidebar is a FULL page load. Without a
// persisted snapshot the new page starts blank and the sidebar's Sources
// group is missing until /api/status returns — officers see an empty console
// for seconds on every bucket switch (2026-11-11).
//
// localStorage is per-origin, so each bucket keeps its own snapshot. The
// first-ever visit to a bucket still has to fetch, but every reload /
// return / tab switch paints the last known data immediately while a fresh
// copy is fetched in the background. No stale data is ever presented as
// live: callers always revalidate and timestamps stay in the payload.

const CACHE_PREFIX = 's3lm_cache_v1:'
const DEFAULT_MAX_AGE_MS = 7 * 24 * 3600 * 1000
const MAX_ENTRY_BYTES = 768 * 1024

export interface CachedValue<T> {
  /** Epoch ms when the value was fetched. */
  t: number
  v: T
}

function storage(): Storage | null {
  try {
    return typeof localStorage !== 'undefined' ? localStorage : null
  } catch {
    return null
  }
}

function cleanParams(params?: Record<string, unknown>): string {
  if (!params) return ''
  const qs = new URLSearchParams()
  for (const k of Object.keys(params).sort()) {
    const v = params[k]
    if (v === undefined || v === null || v === '') continue
    qs.set(k, String(v))
  }
  return qs.toString()
}

export function apiCacheKey(path: string, params?: Record<string, unknown>): string {
  const p = path.startsWith('/api') ? path.slice(4) : path
  const q = cleanParams(params)
  return q ? `${p}?${q}` : p
}

export function readApiCache<T>(
  path: string,
  params?: Record<string, unknown>,
  maxAgeMs = DEFAULT_MAX_AGE_MS,
): CachedValue<T> | null {
  const st = storage()
  if (!st) return null
  const key = CACHE_PREFIX + apiCacheKey(path, params)
  try {
    const raw = st.getItem(key)
    if (!raw) return null
    const ent = JSON.parse(raw) as CachedValue<T>
    if (!ent || typeof ent.t !== 'number') return null
    if (Date.now() - ent.t > maxAgeMs) {
      st.removeItem(key)
      return null
    }
    return ent
  } catch {
    return null
  }
}

function pruneOldest(st: Storage, keep = 20): void {
  const entries: { key: string; at: number }[] = []
  for (let i = 0; i < st.length; i++) {
    const key = st.key(i)
    if (!key || !key.startsWith(CACHE_PREFIX)) continue
    try {
      const raw = st.getItem(key)
      const t = raw ? Number((JSON.parse(raw) as CachedValue<unknown>).t) || 0 : 0
      entries.push({ key, at: t })
    } catch {
      entries.push({ key, at: 0 })
    }
  }
  entries.sort((a, b) => a.at - b.at)
  for (const e of entries.slice(0, Math.max(0, entries.length - keep))) {
    try {
      st.removeItem(e.key)
    } catch {
      break
    }
  }
}

export function writeApiCache<T>(path: string, params: Record<string, unknown> | undefined, value: T): void {
  const st = storage()
  if (!st) return
  const key = CACHE_PREFIX + apiCacheKey(path, params)
  let raw: string
  try {
    raw = JSON.stringify({ t: Date.now(), v: value } satisfies CachedValue<T>)
  } catch {
    return
  }
  if (raw.length > MAX_ENTRY_BYTES) return
  try {
    st.setItem(key, raw)
  } catch {
    // Quota exceeded: drop the oldest dashboard snapshots and retry once.
    try {
      pruneOldest(st)
      st.setItem(key, raw)
    } catch {
      /* private mode / quota — cache is best-effort */
    }
  }
}

export function clearApiCache(): void {
  const st = storage()
  if (!st) return
  try {
    const keys: string[] = []
    for (let i = 0; i < st.length; i++) {
      const k = st.key(i)
      if (k && k.startsWith(CACHE_PREFIX)) keys.push(k)
    }
    for (const k of keys) st.removeItem(k)
  } catch {
    /* ignore */
  }
}
