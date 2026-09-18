export function fmtBytes(n?: number | null): string {
  if (n === undefined || n === null) return '—'
  if (n < 1024) return `${n} B`
  const u = ['KB', 'MB', 'GB', 'TB']
  let i = -1
  let v = n
  do {
    v /= 1024
    i++
  } while (v >= 1024 && i < u.length - 1)
  return `${v.toFixed(v >= 100 ? 0 : 1)} ${u[i]}`
}

export function fmtINR(n?: number | null): string {
  if (n === undefined || n === null) return '—'
  return '₹ ' + n.toLocaleString('en-IN', { maximumFractionDigits: 0 })
}

/** Currency-aware money: crypto principals must never render as ₹. */
export function fmtMoney(n?: number | null, ccy?: string | null): string {
  if (n === undefined || n === null) return '—'
  const c = (ccy || 'INR').toUpperCase()
  if (c === 'INR') return fmtINR(n)
  if (c === 'USD') return '$ ' + n.toLocaleString('en-US', { maximumFractionDigits: 2 })
  // Crypto: trim to significant decimals (69.5 USDT, 0.0023 BTC)
  const s = n.toFixed(8).replace(/\.?0+$/, '')
  return `${s} ${c}`
}

export function fmtTime(ts?: string | null): string {
  if (!ts) return '—'
  return ts.replace('T', ' ').replace('Z', '')
}

const IST_OFFSET_MS = 5.5 * 3600 * 1000
const MON = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']
const pad2 = (n: number) => String(n).padStart(2, '0')

/** Backend timestamps are UTC (bucket last-modified = upload time).
 *  Officers read slips in IST (phone screenshots show IST) — so primary
 *  display is IST: "16 Sep 2026, 06:21 IST". 2026-09-16: a 6:20am GPay slip
 *  showed "modified 2026-09-16 00:51:14" (UTC, unlabeled) and looked wrong —
 *  00:51 UTC *is* 06:21 IST. Never show a bare UTC time again. */
export function fmtIST(ts?: string | null): string {
  if (!ts) return '—'
  const d = new Date(ts)
  if (isNaN(d.getTime())) return ts
  const t = new Date(d.getTime() + IST_OFFSET_MS)
  return `${t.getUTCDate()} ${MON[t.getUTCMonth()]} ${t.getUTCFullYear()}, ${pad2(t.getUTCHours())}:${pad2(t.getUTCMinutes())} IST`
}

/** Dual display for evidence headers: IST first (matches slip screenshots),
 *  UTC in parens (matches bucket/API records). */
export function fmtBoth(ts?: string | null): string {
  if (!ts) return '—'
  return `${fmtIST(ts)} (${fmtTime(ts)} UTC)`
}

export function shortKey(key: string, max = 64): string {
  if (key.length <= max) return key
  return key.slice(0, 24) + ' … ' + key.slice(-36)
}

/** Split GROUP_CONCAT / pipe-joined officer fields (UTRs, holders). */
export function splitJoined(raw?: string | null): string[] {
  if (!raw) return []
  const seen = new Set<string>()
  const out: string[] = []
  for (const part of raw.split(/[|,]/)) {
    const token = part.trim()
    if (token && !seen.has(token)) {
      seen.add(token)
      out.push(token)
    }
  }
  return out
}

export function previewJoined(raw?: string | null, max = 3): { text: string; extra: number; title: string } {
  const list = splitJoined(raw)
  if (!list.length) return { text: '', extra: 0, title: '' }
  return {
    text: list.slice(0, max).join(' · '),
    extra: Math.max(0, list.length - max),
    title: list.join(' · '),
  }
}

const IMG = ['.jpg', '.jpeg', '.png', '.webp', '.gif', '.bmp']
const AUD = ['.wav', '.mp3', '.ogg', '.amr', '.m4a', '.opus', '.webm', '.3gp']
const VID = ['.mp4', '.mov', '.webm', '.avi']
const TXT = ['.txt', '.json', '.csv', '.xml', '.log', '.md', '.html', '.js', '.css']

export function extOf(key: string): string {
  const base = key.split('/').pop() || ''
  const i = base.lastIndexOf('.')
  return i >= 0 ? base.slice(i).toLowerCase() : ''
}

export function kindOf(key: string): 'image' | 'audio' | 'video' | 'text' | 'other' {
  const e = extOf(key)
  if (IMG.includes(e)) return 'image'
  if (AUD.includes(e)) return 'audio'
  if (VID.includes(e)) return 'video'
  if (TXT.includes(e)) return 'text'
  return 'other'
}

// Folder labels — verified against live bucket census 2026-09-11
// (1.56M objects). Each desc says what the folder IS plus what it
// means as evidence, so officers can read it at face value.
const FOLDER_ICONS: Record<string, { icon: string; color: string; desc: string }> = {
  'bankingqr': { icon: '💳', color: '#38bdf8', desc: 'Collection QR codes — mule VPAs customers paid to' },
  'qrcode': { icon: '📱', color: '#34d399', desc: 'Collection QR codes — smaller set, same as bankingqr' },
  'deposit': { icon: '💰', color: '#4ade80', desc: 'Customer deposit slips — payment screenshots, money IN' },
  'withdrawal': { icon: '💸', color: '#f87171', desc: 'Payout proofs — winnings paid OUT to customers' },
  'chat_file': { icon: '💬', color: '#a78bfa', desc: 'Agent–customer chats — voice notes + shared screenshots' },
  'csv-files': { icon: '📊', color: '#fbbf24', desc: 'Operator account books — transactions, bets, P&L, users' },
  'uploads': { icon: '📄', color: '#60a5fa', desc: 'Agent agreements + ID documents (KYC)' },
  'payment': { icon: '🏦', color: '#34d399', desc: 'Single leftover marker file — no evidence' },
  'paymentgateway': { icon: '🔗', color: '#fb923c', desc: 'Gateway-era collection QRs + payout sheets (Oct 24–Aug 25)' },
  'theme-logo': { icon: '🎨', color: '#c084fc', desc: 'Platform theme + branding files' },
  'apk': { icon: '📲', color: '#f472b6', desc: 'Betting app installers (11xworld, apbook365…)' },
  'audio': { icon: '🎙️', color: '#818cf8', desc: 'Platform alert sound — not evidence' },
  'banner': { icon: '🖼️', color: '#fbbf24', desc: 'Promo banners shown to gamblers' },
  'Logo': { icon: '🏷️', color: '#94a3b8', desc: 'Brand logos (CBTF etc.)' },
  'icons': { icon: '✨', color: '#94a3b8', desc: 'Game + UI icons' },
  'notification': { icon: '🔔', color: '#fb923c', desc: 'Bonus/promo announcement images' },
  'undefined': { icon: '❓', color: '#64748b', desc: 'Oldest uploads, never categorized (Dec 23–Jun 24)' },
  'currency': { icon: '💵', color: '#4ade80', desc: 'Single currency icon — not evidence' },
  'all-casino': { icon: '🎰', color: '#f472b6', desc: 'Casino game artwork (live-dealer, roulette…)' },
  'casino-images': { icon: '🃏', color: '#c084fc', desc: 'Card-game artwork (Teenpatti, poker…)' },
  'virtual_games': { icon: '🎮', color: '#22d3ee', desc: 'Virtual-game art + sounds' },
  'st8-gaming': { icon: '🎯', color: '#f59e0b', desc: 'Game-provider artwork (7UpDown, Teenpatti…)' },
  'score-board': { icon: '🏆', color: '#10b981', desc: 'Scoreboard UI icons — not evidence' },
  'ball_by_ball': { icon: '🏏', color: '#06b6d4', desc: 'Ball-by-ball match icons — not evidence' },
  'ball-video': { icon: '🎥', color: '#ef4444', desc: 'Streamed cricket match videos' },
  'level': { icon: '🏅', color: '#a78bfa', desc: 'Player rank badges (bronze, champion…)' },
  'coming-soon': { icon: '⏳', color: '#94a3b8', desc: 'Placeholder art for unreleased games' },
  'aura': { icon: '✨', color: '#c084fc', desc: 'Game-provider artwork batch (Apr 26)' },
}

export function folderInfo(prefix: string): { icon: string; color: string; desc: string } {
  const name = prefix.replace(/\/$/, '')
  return FOLDER_ICONS[name] ?? { icon: '📁', color: '#64748b', desc: name }
}

export function fileIcon(key: string): string {
  const e = extOf(key)
  if (IMG.includes(e)) return '🖼️'
  if (AUD.includes(e)) return '🔊'
  if (VID.includes(e)) return '🎬'
  if (['.csv', '.xlsx', '.xls'].includes(e)) return '📊'
  if (['.pdf'].includes(e)) return '📕'
  if (['.json', '.xml', '.log', '.txt'].includes(e)) return '📝'
  if (e === '.apk') return '📲'
  if (['.doc', '.docx'].includes(e)) return '📄'
  return '📄'
}

export function fileColor(key: string): string {
  const e = extOf(key)
  if (IMG.includes(e)) return '#38bdf8'
  if (AUD.includes(e)) return '#a78bfa'
  if (VID.includes(e)) return '#f87171'
  if (['.csv', '.xlsx', '.xls'].includes(e)) return '#4ade80'
  if (e === '.pdf') return '#f87171'
  if (e === '.json') return '#fbbf24'
  if (e === '.apk') return '#f472b6'
  return '#64748b'
}
