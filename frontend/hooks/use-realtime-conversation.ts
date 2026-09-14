'use client'

import { useCallback, useEffect, useRef, useState } from 'react'
import { clearMessagesCache, loadMessages, saveMessages } from '@/lib/realtime/cache'
import type { ChatMessage } from '@/lib/realtime/types'
import { useWebSocket } from './use-websocket'

/**
 * Collects ASR turns streamed from the PC.
 *
 * - `asr_partial` updates ONE bubble in place (keyed by utterance_id).
 * - `asr_final` marks that bubble final and attaches the persisted message_id.
 * - `history_sync_response` merges without duplicates (dedupe by message_id).
 * - `conversation_cleared` wipes local state + cache.
 */
export function useRealtimeConversation() {
  const { subscribe, status, pcOnline } = useWebSocket()
  const [messages, setMessages] = useState<ChatMessage[]>([])
  const firstRun = useRef(true)

  // Restore the phone cache after mount (kept out of the initial state to avoid
  // an SSR/CSR hydration mismatch).
  useEffect(() => {
    setMessages(loadMessages())
  }, [])

  // Persist changes, skipping the very first run so we never wipe the cache
  // before it has been hydrated above.
  useEffect(() => {
    if (firstRun.current) {
      firstRun.current = false
      return
    }
    saveMessages(messages)
  }, [messages])

  useEffect(
    () =>
      subscribe((event) => {
        switch (event.type) {
          case 'asr_partial':
            setMessages((prev) => {
              const idx = prev.findIndex((m) => m.utteranceId === event.utterance_id)
              if (idx >= 0) {
                const copy = [...prev]
                copy[idx] = { ...copy[idx], text: event.text, speaker: event.speaker }
                return copy
              }
              return [
                ...prev,
                {
                  id: event.utterance_id,
                  utteranceId: event.utterance_id,
                  messageId: null,
                  speaker: event.speaker,
                  text: event.text,
                  timestamp: Date.now(),
                  durationSec: null,
                  isFinal: false,
                },
              ]
            })
            break

          case 'asr_final':
            setMessages((prev) => {
              const idx = prev.findIndex((m) => m.utteranceId === event.utterance_id)
              if (idx >= 0) {
                const copy = [...prev]
                copy[idx] = {
                  ...copy[idx],
                  text: event.text,
                  speaker: event.speaker,
                  messageId: event.message_id,
                  isFinal: true,
                  timestamp: event.created_at,
                  durationSec: event.duration_sec,
                }
                return copy
              }
              if (prev.some((m) => m.messageId === event.message_id)) return prev
              return [
                ...prev,
                {
                  id: event.message_id,
                  utteranceId: event.utterance_id,
                  messageId: event.message_id,
                  speaker: event.speaker,
                  text: event.text,
                  timestamp: event.created_at,
                  durationSec: event.duration_sec,
                  isFinal: true,
                },
              ]
            })
            break

          case 'history_sync_response':
            setMessages((prev) => {
              const seen = new Set(
                prev.map((m) => m.messageId).filter((id): id is string => Boolean(id)),
              )
              const added: ChatMessage[] = []
              for (const hm of event.payload.messages) {
                if (!hm.message_id || seen.has(hm.message_id)) continue
                seen.add(hm.message_id)
                added.push({
                  id: hm.message_id,
                  utteranceId: hm.message_id,
                  messageId: hm.message_id,
                  speaker: hm.speaker,
                  text: hm.text,
                  timestamp: hm.created_at,
                  durationSec: hm.duration_sec ?? null,
                  isFinal: true,
                })
              }
              if (added.length === 0) return prev
              return [...prev, ...added].sort((a, b) => a.timestamp - b.timestamp)
            })
            break

          case 'conversation_cleared':
            clearMessagesCache()
            setMessages([])
            break
        }
      }),
    [subscribe],
  )

  const clear = useCallback(() => {
    clearMessagesCache()
    setMessages([])
  }, [])

  return { messages, isListening: pcOnline && status === 'connected', clear }
}
