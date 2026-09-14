'use client'

import { useCallback, useEffect, useRef, useState } from 'react'
import type { AnswerStatus, LlmMode } from '@/lib/realtime/types'
import { randomId } from '@/lib/realtime/util'
import { useWebSocket } from './use-websocket'

export interface AnswerState {
  requestId: string
  mode: LlmMode
  /** message_id (conversation) or screenshot_id (screenshot) this answer is for. */
  targetId: string | null
  text: string
  status: AnswerStatus
  errorMessage?: string
  tokensPerSecond: number | null
  /** epoch ms, when the answer card first appeared. */
  timestamp: number
}

/**
 * Real streaming-answer store, keyed by request_id.
 *
 * Accumulates `llm_chunk` deltas, tracks `llm_stats`/`llm_done` speed, and
 * resolves to done/stopped/error on the terminal events. Enforces a single
 * active request (PRD §9): `start()` is a no-op while one is in flight.
 */
export function useStreamingAnswer() {
  const { subscribe, send } = useWebSocket()
  const [answers, setAnswers] = useState<Record<string, AnswerState>>({})
  const [activeRequestId, setActiveRequestId] = useState<string | null>(null)
  const activeRef = useRef<string | null>(null)
  activeRef.current = activeRequestId

  useEffect(
    () =>
      subscribe((event) => {
        switch (event.type) {
          case 'llm_started':
            setAnswers((prev) => {
              const existing = prev[event.request_id]
              if (existing) {
                return {
                  ...prev,
                  [event.request_id]: { ...existing, mode: event.mode, status: 'streaming' },
                }
              }
              return {
                ...prev,
                [event.request_id]: {
                  requestId: event.request_id,
                  mode: event.mode,
                  targetId: null,
                  text: '',
                  status: 'streaming',
                  tokensPerSecond: null,
                  timestamp: Date.now(),
                },
              }
            })
            setActiveRequestId(event.request_id)
            break

          case 'llm_chunk':
            setAnswers((prev) => {
              const a = prev[event.request_id]
              if (!a) return prev
              return { ...prev, [event.request_id]: { ...a, text: a.text + event.delta } }
            })
            break

          case 'llm_stats':
            setAnswers((prev) => {
              const a = prev[event.request_id]
              if (!a) return prev
              return {
                ...prev,
                [event.request_id]: { ...a, tokensPerSecond: event.tokens_per_second },
              }
            })
            break

          case 'llm_done':
            setAnswers((prev) => {
              const a = prev[event.request_id]
              if (!a) return prev
              return {
                ...prev,
                [event.request_id]: {
                  ...a,
                  status: 'done',
                  tokensPerSecond: event.tokens_per_second,
                },
              }
            })
            if (activeRef.current === event.request_id) setActiveRequestId(null)
            break

          case 'llm_cancelled':
            setAnswers((prev) => {
              const a = prev[event.request_id]
              if (!a) return prev
              // Keep the partial text; just mark it stopped (PRD §11).
              return { ...prev, [event.request_id]: { ...a, status: 'stopped' } }
            })
            if (activeRef.current === event.request_id) setActiveRequestId(null)
            break

          case 'llm_error':
            setAnswers((prev) => {
              const a = prev[event.request_id]
              if (!a) return prev
              return {
                ...prev,
                [event.request_id]: { ...a, status: 'error', errorMessage: event.message },
              }
            })
            if (activeRef.current === event.request_id) setActiveRequestId(null)
            break
        }
      }),
    [subscribe],
  )

  const start = useCallback(
    (mode: LlmMode, targetId: string | null): string | null => {
      if (activeRef.current) return null // single active request at a time
      const requestId = randomId()
      setAnswers((prev) => ({
        ...prev,
        [requestId]: {
          requestId,
          mode,
          targetId,
          text: '',
          status: 'streaming',
          tokensPerSecond: null,
          timestamp: Date.now(),
        },
      }))
      setActiveRequestId(requestId)
      send({
        type: 'llm_request',
        payload: {
          request_id: requestId,
          mode,
          message_id: mode === 'conversation' ? targetId : null,
          screenshot_id: mode === 'screenshot' ? targetId : null,
        },
      })
      return requestId
    },
    [send],
  )

  const stop = useCallback(() => {
    if (activeRef.current) send({ type: 'llm_cancel', payload: { request_id: activeRef.current } })
  }, [send])

  const reset = useCallback(() => {
    setAnswers({})
    setActiveRequestId(null)
  }, [])

  const active = activeRequestId ? answers[activeRequestId] : undefined
  const isStreaming = active?.status === 'streaming'
  const tokensPerSecond = active?.tokensPerSecond ?? null

  return { answers, activeRequestId, isStreaming, start, stop, reset, tokensPerSecond }
}
