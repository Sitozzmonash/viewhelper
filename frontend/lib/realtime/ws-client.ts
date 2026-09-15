import { getDeviceId, loadPairing } from './pairing'
import type {
  ConnectionStatus,
  OutgoingCommand,
  RealtimeEvent,
  RealtimeListener,
  RealtimeTransport,
  SettingsPayload,
} from './types'
import { isBrowser, randomId } from './util'

/** Exponential backoff (ms): 1,2,4,8,16,30 then 30 forever — PRD §21 / README §5. */
const BACKOFF_STEPS = [1000, 2000, 4000, 8000, 16000, 30000]
const MAX_BACKOFF_MS = 30000

/** Loose shape of an inbound envelope (see shared/protocol/events.schema.json). */
interface WireEnvelope {
  type?: string
  event_id?: string
  device_id?: string
  timestamp?: number
  payload?: Record<string, unknown>
}

/**
 * Real WebSocket transport for the mobile client.
 *
 * - Connects to the relay URL, sends `auth {role:"mobile", room, token}` first.
 * - Reconnects with exponential backoff; on every (re)auth it re-requests
 *   history via `history_sync_request`.
 * - Parses inbound envelopes per the shared schema and emits typed events.
 * - Wraps outbound commands in the full protocol envelope (the relay validates
 *   event_id/device_id/timestamp for mobile -> pc messages).
 */
export class WsClient implements RealtimeTransport {
  private readonly url: string
  private readonly deviceId: string
  private ws: WebSocket | null = null
  private listeners = new Set<RealtimeListener>()
  private status: ConnectionStatus = 'disconnected'
  private backoffIndex = 0
  private reconnectTimer: ReturnType<typeof setTimeout> | null = null
  private manualClose = false

  constructor(url: string) {
    this.url = url
    this.deviceId = getDeviceId()
  }

  connect(): void {
    this.manualClose = false
    this.backoffIndex = 0
    this.openSocket()
  }

  disconnect(): void {
    this.manualClose = true
    if (this.reconnectTimer) {
      clearTimeout(this.reconnectTimer)
      this.reconnectTimer = null
    }
    if (this.ws) {
      this.ws.onopen = null
      this.ws.onmessage = null
      this.ws.onerror = null
      this.ws.onclose = null
      try {
        this.ws.close()
      } catch {
        /* ignore */
      }
      this.ws = null
    }
    this.setStatus('disconnected')
  }

  subscribe(listener: RealtimeListener): () => void {
    this.listeners.add(listener)
    return () => {
      this.listeners.delete(listener)
    }
  }

  send(command: OutgoingCommand): void {
    const payload = 'payload' in command ? (command.payload as Record<string, unknown>) : undefined
    this.dispatch(command.type, payload)
  }

  /* ------------------------------------------------------------- internals */

  private openSocket(): void {
    if (!isBrowser()) return
    this.setStatus('connecting')

    let socket: WebSocket
    try {
      socket = new WebSocket(this.url)
    } catch {
      this.scheduleReconnect()
      return
    }
    this.ws = socket

    socket.onopen = () => {
      this.backoffIndex = 0
      const { room, token } = loadPairing()
      this.dispatch('auth', { role: 'mobile', room, token })
    }
    socket.onmessage = (ev: MessageEvent) => this.handleMessage(ev)
    socket.onerror = () => {
      /* onclose always follows; reconnect is handled there */
    }
    socket.onclose = () => {
      if (this.ws === socket) this.ws = null
      this.setStatus('disconnected')
      if (!this.manualClose) this.scheduleReconnect()
    }
  }

  private scheduleReconnect(): void {
    if (this.manualClose || this.reconnectTimer) return
    const delay =
      this.backoffIndex < BACKOFF_STEPS.length ? BACKOFF_STEPS[this.backoffIndex] : MAX_BACKOFF_MS
    this.backoffIndex += 1
    this.reconnectTimer = setTimeout(() => {
      this.reconnectTimer = null
      this.openSocket()
    }, delay)
  }

  private dispatch(type: string, payload?: Record<string, unknown>): void {
    if (!this.ws || this.ws.readyState !== WebSocket.OPEN) return
    const envelope = {
      type,
      event_id: randomId(),
      device_id: this.deviceId,
      timestamp: Date.now(),
      payload: payload ?? {},
    }
    try {
      this.ws.send(JSON.stringify(envelope))
    } catch {
      /* a failed send surfaces via onclose -> reconnect */
    }
  }

  private handleMessage(ev: MessageEvent): void {
    if (typeof ev.data !== 'string') return
    let data: WireEnvelope
    try {
      data = JSON.parse(ev.data) as WireEnvelope
    } catch {
      return // ignore malformed frames
    }
    if (!data || typeof data.type !== 'string') return
    const p = (data.payload ?? {}) as Record<string, unknown>
    const str = (v: unknown, fallback = ''): string => (typeof v === 'string' ? v : fallback)
    const num = (v: unknown, fallback = 0): number => (typeof v === 'number' ? v : fallback)
    const speaker = (v: unknown): 'me' | 'other' => (v === 'me' ? 'me' : 'other')

    switch (data.type) {
      case 'auth_ok':
        this.setStatus('connected')
        this.emit({ type: 'auth_ok', pcOnline: Boolean(p.pc_online) })
        // Re-sync history after every (re)connect + auth (README §5, PRD §21).
        this.send({ type: 'history_sync_request', payload: { limit: 100 } })
        break

      case 'presence':
        this.emit({ type: 'presence', pcOnline: Boolean(p.pc_online) })
        break

      case 'asr_partial':
        if (typeof p.utterance_id === 'string') {
          this.emit({
            type: 'asr_partial',
            utterance_id: p.utterance_id,
            speaker: speaker(p.speaker),
            text: str(p.text),
          })
        }
        break

      case 'asr_final':
        if (typeof p.message_id === 'string') {
          this.emit({
            type: 'asr_final',
            message_id: p.message_id,
            utterance_id: str(p.utterance_id, p.message_id),
            speaker: speaker(p.speaker),
            text: str(p.text),
            created_at: num(p.created_at, Date.now()),
            duration_sec: typeof p.duration_sec === 'number' ? p.duration_sec : null,
          })
        }
        break

      case 'history_sync_response':
        this.emit({
          type: 'history_sync_response',
          payload: {
            messages: Array.isArray(p.messages) ? p.messages : [],
            screenshots: Array.isArray(p.screenshots) ? p.screenshots : [],
            ai_answers: Array.isArray(p.ai_answers) ? p.ai_answers : [],
          },
        })
        break

      case 'llm_started':
        this.emit({
          type: 'llm_started',
          request_id: str(p.request_id),
          mode: p.mode === 'screenshot' ? 'screenshot' : 'conversation',
          target_id: typeof p.target_id === 'string' ? p.target_id : null,
        })
        break

      case 'llm_chunk':
        this.emit({ type: 'llm_chunk', request_id: str(p.request_id), delta: str(p.delta) })
        break

      case 'llm_stats':
        this.emit({
          type: 'llm_stats',
          request_id: str(p.request_id),
          tokens_per_second: num(p.tokens_per_second),
          elapsed_ms: typeof p.elapsed_ms === 'number' ? p.elapsed_ms : undefined,
          estimated_tokens: typeof p.estimated_tokens === 'number' ? p.estimated_tokens : undefined,
        })
        break

      case 'llm_done':
        this.emit({
          type: 'llm_done',
          request_id: str(p.request_id),
          completion_tokens: num(p.completion_tokens),
          elapsed_ms: num(p.elapsed_ms),
          tokens_per_second: num(p.tokens_per_second),
        })
        break

      case 'llm_cancelled':
        this.emit({ type: 'llm_cancelled', request_id: str(p.request_id) })
        break

      case 'llm_error':
        this.emit({
          type: 'llm_error',
          request_id: str(p.request_id),
          message: str(p.message, '生成失败'),
        })
        break

      case 'screenshot_created':
        this.emit({
          type: 'screenshot_created',
          screenshot_id: str(p.screenshot_id),
          preview: str(p.preview),
          captured_at: num(p.captured_at, Date.now()),
        })
        break

      case 'conversation_cleared':
        this.emit({ type: 'conversation_cleared' })
        break

      case 'screenshot_cleared':
        this.emit({ type: 'screenshot_cleared' })
        break

      case 'settings_updated':
        this.emit({ type: 'settings_updated', settings: p as SettingsPayload })
        break

      case 'error':
        this.emit({
          type: 'error',
          message: str(p.message, '连接错误'),
          code: typeof p.code === 'string' ? p.code : undefined,
        })
        break

      default:
        break // unknown types must be ignored, not crash (README §Rules)
    }
  }

  private emit(event: RealtimeEvent): void {
    this.listeners.forEach((listener) => listener(event))
  }

  private setStatus(status: ConnectionStatus): void {
    if (this.status === status) return
    this.status = status
    this.emit({ type: 'status', status })
  }
}
