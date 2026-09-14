'use client'

import { useRealtimeContext } from '@/components/realtime-provider'

/**
 * The single seam between the UI and the transport.
 *
 * Today it reads from the mock `RealtimeClient`; pointing it at a real
 * WebSocket only requires changing the provider, not any consumer.
 */
export function useWebSocket() {
  return useRealtimeContext()
}
