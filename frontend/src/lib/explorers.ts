// Shared on-chain explorer deep-links (CASE-2026-CBTF).
// Every crypto wallet shown in the dashboard links out to:
//   - Arkham (address view) — platform.arkhamintelligence.com/explorer/address/{addr}
//   - Tronscan — tx view when a 64-hex txid was extracted, else address view.
// URL shapes verified 2026-09-05:
//   Arkham addr: https://platform.arkhamintelligence.com/explorer/address/<addr>
//   Arkham tx:   https://platform.arkhamintelligence.com/explorer/tx/<txid>
//   Tronscan addr: https://tronscan.org/#/address/<addr>
//   Tronscan tx:   https://tronscan.org/#/transaction/<txid>

export function arkhamAddressUrl(address: string): string {
  return `https://platform.arkhamintelligence.com/explorer/address/${encodeURIComponent(address.trim())}`
}

export function arkhamTxUrl(txid: string): string {
  return `https://platform.arkhamintelligence.com/explorer/tx/${encodeURIComponent(txid.trim().toLowerCase())}`
}

export function tronscanAddressUrl(address: string): string {
  return `https://tronscan.org/#/address/${encodeURIComponent(address.trim())}`
}

export function tronscanTxUrl(txid: string): string {
  return `https://tronscan.org/#/transaction/${encodeURIComponent(txid.trim().toLowerCase())}`
}

export type Txids = string[] | string | null | undefined

export function firstTxid(txids: Txids): string | null {
  if (!txids) return null
  if (Array.isArray(txids)) {
    for (const t of txids) {
      const s = String(t ?? '').trim()
      if (/^[0-9a-fA-F]{64}$/.test(s)) return s.toLowerCase()
    }
    return null
  }
  // backend sometimes stores JSON-encoded '["..."]' string or csv
  const raw = String(txids).trim()
  if (!raw) return null
  try {
    const parsed: unknown = JSON.parse(raw)
    if (Array.isArray(parsed)) return firstTxid(parsed as string[])
    if (typeof parsed === 'string') return firstTxid(parsed)
  } catch {
    /* not JSON — fall through to csv/hex handling */
  }
  for (const part of raw.split(/[,\s]+/)) {
    const s = part.trim()
    if (/^[0-9a-fA-F]{64}$/.test(s)) return s.toLowerCase()
  }
  return null
}

/** Best Tronscan target: tx view when a txid exists, else address view. */
export function bestTronscanUrl(address?: string | null, txids?: Txids): string | null {
  const tx = firstTxid(txids)
  if (tx) return tronscanTxUrl(tx)
  if (address && String(address).trim()) return tronscanAddressUrl(String(address))
  return null
}

/** Tronscan link label: "tx" when pointing at a transaction, else "addr". */
export function tronscanLabel(txids?: Txids): string {
  return firstTxid(txids) ? 'Tronscan tx ↗' : 'Tronscan ↗'
}
