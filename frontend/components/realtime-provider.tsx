'use client'

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
} from 'react'
import { MockRealtimeClient } from '@/lib/realtime/mock-socket'
import { loadPairing, savePairing } from '@/lib/realtime/pairing'
import type {
  ConnectionStatus,
  OutgoingCommand,
  PairingConfig,
  RealtimeEvent,
  RealtimeListener,
  RealtimeTransport,
} from '@/lib/realtime/types'
import { WsClient } from '@/lib/realtime/ws-client'

const RELAY_URL = (process.env.NEXT_PUBLIC_RELAY_URL ?? '').trim()
const DEFAULT_RELAY_URL = 'ws://localhost:8000/ws'
/**
 * Demo mode: no backend needed. Used when NEXT_PUBLIC_DEMO=1 or when no relay
 * URL is configured, so the UI can always be demonstrated.
 */
const DEMO = process.env.NEXT_PUBLIC_DEMO === '1' || RELAY_URL === ''

interface RealtimeContextValue {
  /** Mobile <-> relay socket lifecycle. */
  status: ConnectionStatus
  /** PC presence, driven by `presence` and `auth_ok.pc_online`. */
  pcOnline: boolean
  demo: boolean
  pairing: PairingConfig
  setPairing: (config: PairingConfig) => void
  subscribe: (listener: RealtimeListener) => () => void
  send: (command: OutgoingCommand) => void
}

const RealtimeContext = createContext<RealtimeContextValue | null>(null)

export function RealtimeProvider({ children }: { children: React.ReactNode }) {
  const [status, setStatus] = useState<ConnectionStatus>('connecting')
  const [pcOnline, setPcOnline] = useState(false)
  const [pairing, setPairingState] = useState<PairingConfig>(() => loadPairing())
  const listenersRef = useRef(new Set<RealtimeListener>())
  const transportRef = useRef<RealtimeTransport | null>(null)

  const subscribe = useCallback((listener: RealtimeListener) => {
    listenersRef.current.add(listener)
    return () => {
      listenersRef.current.delete(listener)
    }
  }, [])

  const send = useCallback((command: OutgoingCommand) => {
    transportRef.current?.send(command)
  }, [])

  const setPairing = useCallback((config: PairingConfig) => {
    const next = { room: config.room.trim(), token: config.token.trim() }
    savePairing(next)
    // Updating state re-runs the transport effect (deps below) -> reconnect.
    setPairingState(next)
  }, [])

  useEffect(() => {
    const transport: RealtimeTransport = DEMO
      ? new MockRealtimeClient()
      : new WsClient(RELAY_URL || DEFAULT_RELAY_URL)
    transportRef.current = transport

    const unsubscribe = transport.subscribe((event: RealtimeEvent) => {
      if (event.type === 'status') setStatus(event.status)
      else if (event.type === 'presence' || event.type === 'auth_ok') setPcOnline(event.pcOnline)
      listenersRef.current.forEach((listener) => listener(event))
    })

    transport.connect()

    return () => {
      unsubscribe()
      transport.disconnect()
      if (transportRef.current === transport) transportRef.current = null
    }
  }, [pairing.room, pairing.token])

  const value = useMemo<RealtimeContextValue>(
    () => ({ status, pcOnline, demo: DEMO, pairing, setPairing, subscribe, send }),
    [status, pcOnline, pairing, setPairing, subscribe, send],
  )

  return <RealtimeContext.Provider value={value}>{children}</RealtimeContext.Provider>
}

export function useRealtimeContext() {
  const context = useContext(RealtimeContext)
  if (!context) {
    throw new Error('useRealtimeContext 必须在 RealtimeProvider 内部使用')
  }
  return context
}
