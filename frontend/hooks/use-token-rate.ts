'use client'

import { useEffect, useState } from 'react'
import { useWebSocket } from './use-websocket'

/**
 * Live tokens-per-second readout (PRD §12).
 *
 * Driven by `llm_stats` (about every 500ms while streaming) and the final
 * `llm_done` value. Each new request resets it so an idle UI shows
 * "-- token/s" (the caller renders null as "--").
 */
export function useTokenRate(): number | null {
  const { subscribe } = useWebSocket()
  const [rate, setRate] = useState<number | null>(null)

  useEffect(
    () =>
      subscribe((event) => {
        if (event.type === 'llm_started') setRate(null)
        else if (event.type === 'llm_stats') setRate(event.tokens_per_second)
        else if (event.type === 'llm_done') setRate(event.tokens_per_second)
      }),
    [subscribe],
  )

  return rate
}
