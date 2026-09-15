/**
 * Realtime transport types.
 *
 * These mirror the authoritative wire protocol in `shared/protocol/README.md`
 * and `shared/protocol/events.schema.json`. All wire timestamps are UTC epoch
 * milliseconds; the UI converts them to local time for display.
 */

/** Mobile <-> relay socket lifecycle. */
export type ConnectionStatus = 'connecting' | 'connected' | 'disconnected'

/** ASR speaker: the local user ("我") or the far end ("对方"). */
export type Speaker = 'other' | 'me'

/** Which LLM pipeline produced an answer. */
export type LlmMode = 'conversation' | 'screenshot'

/** Lifecycle of a single AI answer on the client. */
export type AnswerStatus = 'streaming' | 'done' | 'stopped' | 'error'

/** Pairing credentials stored locally on the phone (never the LLM API key). */
export interface PairingConfig {
  room: string
  token: string
}

/**
 * Editable settings that live on the PC (`config.yaml`). API keys are NEVER
 * part of this payload — see PRD §8.3 / §22.
 */
export interface SettingsPayload {
  conversation_prompt?: string
  screenshot_prompt?: string
  context_messages?: number
  hotwords?: string
  show_partial?: boolean
}

/* ------------------------------------------------------------------ domain */

export interface ChatMessage {
  /** Stable React key: the utterance id while pending, message id otherwise. */
  id: string
  utteranceId: string
  /** Present once the turn is final (persisted on the PC). */
  messageId: string | null
  speaker: Speaker
  text: string
  /** epoch ms */
  timestamp: number
  /** ASR clip length in seconds, when known. */
  durationSec: number | null
  isFinal: boolean
}

export interface Screenshot {
  id: string
  /** epoch ms the screenshot was captured on the PC. */
  capturedAt: number
  /** Compressed preview as a data-url (only the latest screenshot has one). */
  preview: string | null
}

/* ------------------------------------------------------- history sync rows */

export interface HistoryMessage {
  message_id: string
  speaker: Speaker
  text: string
  created_at: number
  duration_sec?: number | null
}

export interface HistoryScreenshot {
  screenshot_id: string
  preview: string | null
  created_at: number
}

export interface HistoryAnswer {
  request_id: string
  mode: LlmMode
  target_id: string | null
  answer: string
  status: 'done' | 'cancelled' | 'error'
  tokens_per_second: number | null
  created_at: number
}

export interface HistorySyncPayload {
  messages: HistoryMessage[]
  screenshots: HistoryScreenshot[]
  ai_answers: HistoryAnswer[]
}

/* ------------------------------------------------------------ inbound events */

/**
 * Typed events a transport emits to the UI. Both the real WebSocket client and
 * the demo mock produce exactly this shape, so consumers are transport-agnostic.
 */
export type RealtimeEvent =
  | { type: 'status'; status: ConnectionStatus }
  | { type: 'presence'; pcOnline: boolean }
  | { type: 'auth_ok'; pcOnline: boolean }
  | { type: 'asr_partial'; utterance_id: string; speaker: Speaker; text: string }
  | {
      type: 'asr_final'
      message_id: string
      utterance_id: string
      speaker: Speaker
      text: string
      created_at: number
      duration_sec: number | null
    }
  | { type: 'history_sync_response'; payload: HistorySyncPayload }
  | { type: 'llm_started'; request_id: string; mode: LlmMode; target_id?: string | null }
  | { type: 'llm_chunk'; request_id: string; delta: string }
  | {
      type: 'llm_stats'
      request_id: string
      tokens_per_second: number
      elapsed_ms?: number
      estimated_tokens?: number
    }
  | {
      type: 'llm_done'
      request_id: string
      completion_tokens: number
      elapsed_ms: number
      tokens_per_second: number
    }
  | { type: 'llm_cancelled'; request_id: string }
  | { type: 'llm_error'; request_id: string; message: string }
  | { type: 'screenshot_created'; screenshot_id: string; preview: string; captured_at: number }
  | { type: 'conversation_cleared' }
  | { type: 'screenshot_cleared' }
  | { type: 'settings_updated'; settings: SettingsPayload }
  | { type: 'error'; message: string; code?: string }

export type RealtimeListener = (event: RealtimeEvent) => void

/* ----------------------------------------------------------- outbound commands */

/**
 * Commands the mobile client sends (mobile -> pc, plus the relay-facing ones).
 * The transport wraps each in the protocol envelope before it hits the wire.
 */
export type OutgoingCommand =
  | {
      type: 'llm_request'
      payload: {
        request_id: string
        mode: LlmMode
        message_id?: string | null
        screenshot_id?: string | null
      }
    }
  | { type: 'llm_cancel'; payload: { request_id: string } }
  | { type: 'capture_screen' }
  | { type: 'screenshot_delete'; payload: { screenshot_id: string } }
  | { type: 'conversation_clear' }
  | { type: 'screenshot_clear' }
  | { type: 'history_sync_request'; payload?: { limit?: number } }
  | { type: 'settings_get' }
  | { type: 'settings_update'; payload: SettingsPayload }

/** The seam every transport (real socket, demo mock) implements. */
export interface RealtimeTransport {
  connect(): void
  disconnect(): void
  subscribe(listener: RealtimeListener): () => void
  send(command: OutgoingCommand): void
}
