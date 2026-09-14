import type { ChatMessage, Screenshot } from './types'
import { isBrowser } from './util'

/**
 * Phone-side cache (PRD §19.2). The PC's SQLite stays the source of truth and
 * is re-synced via history_sync_request on every (re)connect; this localStorage
 * cache only smooths over page refreshes / brief offline moments. It is wiped
 * on conversation_cleared / screenshot_cleared.
 */

const MESSAGES_KEY = 'viewhelper-cache-messages-v1'
const SCREENSHOTS_KEY = 'viewhelper-cache-screenshots-v1'
const MAX_MESSAGES = 200
const MAX_SCREENSHOTS = 6

export function loadMessages(): ChatMessage[] {
  if (!isBrowser()) return []
  try {
    const raw = window.localStorage.getItem(MESSAGES_KEY)
    if (!raw) return []
    const parsed = JSON.parse(raw)
    return Array.isArray(parsed) ? (parsed as ChatMessage[]) : []
  } catch {
    return []
  }
}

export function saveMessages(messages: ChatMessage[]): void {
  if (!isBrowser()) return
  try {
    window.localStorage.setItem(MESSAGES_KEY, JSON.stringify(messages.slice(-MAX_MESSAGES)))
  } catch {
    /* ignore quota errors */
  }
}

export function clearMessagesCache(): void {
  if (!isBrowser()) return
  try {
    window.localStorage.removeItem(MESSAGES_KEY)
  } catch {
    /* ignore */
  }
}

export function loadScreenshots(): Screenshot[] {
  if (!isBrowser()) return []
  try {
    const raw = window.localStorage.getItem(SCREENSHOTS_KEY)
    if (!raw) return []
    const parsed = JSON.parse(raw)
    return Array.isArray(parsed) ? (parsed as Screenshot[]) : []
  } catch {
    return []
  }
}

export function saveScreenshots(shots: Screenshot[]): void {
  if (!isBrowser()) return
  try {
    // Keep the newest few, and drop the heavy preview from all but the latest
    // so we stay far inside the ~5MB localStorage budget.
    const slice = shots
      .slice(0, MAX_SCREENSHOTS)
      .map((s, i) => (i === 0 ? s : { ...s, preview: null }))
    window.localStorage.setItem(SCREENSHOTS_KEY, JSON.stringify(slice))
  } catch {
    /* ignore quota errors (previews can be large) */
  }
}

export function clearScreenshotsCache(): void {
  if (!isBrowser()) return
  try {
    window.localStorage.removeItem(SCREENSHOTS_KEY)
  } catch {
    /* ignore */
  }
}

/** Clears every phone-side cache entry (used after a clear ack). */
export function clearAllCache(): void {
  clearMessagesCache()
  clearScreenshotsCache()
}
