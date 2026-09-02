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

export function fmtTime(ts?: string | null): string {
  if (!ts) return '—'
  return ts.replace('T', ' ').replace('Z', '')
}

export function shortKey(key: string, max = 64): string {
  if (key.length <= max) return key
  return key.slice(0, 24) + ' … ' + key.slice(-36)
}

const IMG = ['.jpg', '.jpeg', '.png', '.webp', '.gif', '.bmp']
const AUD = ['.wav', '.mp3', '.ogg', '.amr']
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

// Folder icons — map prefix names to contextual icons for investigation
const FOLDER_ICONS: Record<string, { icon: string; color: string; desc: string }> = {
  'bankingqr': { icon: '💳', color: '#38bdf8', desc: 'Payment QRs' },
  'qrcode': { icon: '📱', color: '#34d399', desc: 'QR Codes' },
  'deposit': { icon: '💰', color: '#4ade80', desc: 'Deposit Slips' },
  'withdrawal': { icon: '💸', color: '#f87171', desc: 'Withdrawal Proofs' },
  'chat_file': { icon: '💬', color: '#a78bfa', desc: 'Chat Messages' },
  'csv-files': { icon: '📊', color: '#fbbf24', desc: 'Transaction Logs' },
  'uploads': { icon: '📄', color: '#60a5fa', desc: 'KYC Documents' },
  'payment': { icon: '🏦', color: '#34d399', desc: 'Payment Proofs' },
  'paymentgateway': { icon: '🔗', color: '#fb923c', desc: 'Gateway Receipts' },
  'theme-logo': { icon: '🎨', color: '#c084fc', desc: 'Platform Config' },
  'apk': { icon: '📲', color: '#f472b6', desc: 'App Installers' },
  'audio': { icon: '🎙️', color: '#818cf8', desc: 'Voice Notes' },
  'banner': { icon: '🖼️', color: '#fbbf24', desc: 'Marketing Banners' },
  'Logo': { icon: '🏷️', color: '#94a3b8', desc: 'Brand Assets' },
  'icons': { icon: '✨', color: '#94a3b8', desc: 'UI Icons' },
  'notification': { icon: '🔔', color: '#fb923c', desc: 'Notifications' },
  'undefined': { icon: '❓', color: '#64748b', desc: 'Unclassified' },
  'currency': { icon: '💵', color: '#4ade80', desc: 'Currency' },
  'all-casino': { icon: '🎰', color: '#f472b6', desc: 'Casino Images' },
  'casino-images': { icon: '🃏', color: '#c084fc', desc: 'Casino Content' },
  'virtual_games': { icon: '🎮', color: '#22d3ee', desc: 'Virtual Games' },
  'st8-gaming': { icon: '🎯', color: '#f59e0b', desc: 'Gaming Content' },
  'score-board': { icon: '🏆', color: '#10b981', desc: 'Scoreboard' },
  'ball_by_ball': { icon: '🏏', color: '#06b6d4', desc: 'Live Ball Data' },
  'ball-video': { icon: '🎥', color: '#ef4444', desc: 'Ball Videos' },
  'level': { icon: '🏅', color: '#a78bfa', desc: 'User Levels' },
  'coming-soon': { icon: '⏳', color: '#94a3b8', desc: 'Coming Soon' },
  'aura': { icon: '✨', color: '#c084fc', desc: 'Aura Assets' },
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
