import type { PairingConfig } from './types'
import { isBrowser, randomId } from './util'

const PAIRING_KEY = 'viewhelper-pairing-v1'
const DEVICE_KEY = 'viewhelper-device-id-v1'

const EMPTY: PairingConfig = { room: '', token: '' }

/**
 * Pairing credentials (room + relay token) live only in the phone's
 * localStorage. The LLM API key is NEVER stored here — see PRD §8.3 / §22.
 */
export function loadPairing(): PairingConfig {
  if (!isBrowser()) return { ...EMPTY }
  try {
    const raw = window.localStorage.getItem(PAIRING_KEY)
    if (!raw) return { ...EMPTY }
    const parsed = JSON.parse(raw) as Partial<PairingConfig>
    return { room: String(parsed.room ?? ''), token: String(parsed.token ?? '') }
  } catch {
    return { ...EMPTY }
  }
}

export function savePairing(config: PairingConfig): void {
  if (!isBrowser()) return
  try {
    window.localStorage.setItem(
      PAIRING_KEY,
      JSON.stringify({ room: config.room, token: config.token }),
    )
  } catch {
    /* ignore quota / privacy-mode errors */
  }
}

/** Stable per-install device id used in the protocol envelope's device_id. */
export function getDeviceId(): string {
  if (!isBrowser()) return 'mobile-ssr'
  try {
    let id = window.localStorage.getItem(DEVICE_KEY)
    if (!id) {
      id = `mobile-${randomId()}`
      window.localStorage.setItem(DEVICE_KEY, id)
    }
    return id
  } catch {
    return 'mobile-unknown'
  }
}
