/** Small runtime helpers shared by the realtime transports and hooks. */

/** True when running in a browser-like environment (not during SSR/prerender). */
export function isBrowser(): boolean {
  return typeof window !== 'undefined'
}

/**
 * RFC4122 v4 id. Uses the Web Crypto API when available and falls back to a
 * Math.random-based generator so it never throws in constrained environments.
 */
export function randomId(): string {
  const c = globalThis.crypto as Crypto | undefined
  if (c && typeof c.randomUUID === 'function') return c.randomUUID()
  // Fallback (still 128 bits of randomness, formatted as a uuid v4).
  const bytes = new Uint8Array(16)
  if (c && typeof c.getRandomValues === 'function') {
    c.getRandomValues(bytes)
  } else {
    for (let i = 0; i < 16; i += 1) bytes[i] = Math.floor(Math.random() * 256)
  }
  bytes[6] = (bytes[6] & 0x0f) | 0x40
  bytes[8] = (bytes[8] & 0x3f) | 0x80
  const hex = Array.from(bytes, (b) => b.toString(16).padStart(2, '0')).join('')
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`
}
