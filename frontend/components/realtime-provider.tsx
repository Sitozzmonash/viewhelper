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
const RELAY_PORT = 8000

/**
 * Demo mode is opt-in via NEXT_PUBLIC_DEMO=1 only. An empty relay URL no longer
 * means "demo" — it means "derive the relay from the page host at runtime".
 */
const DEMO = process.env.NEXT_PUBLIC_DEMO === '1'

/**
 * Resolve the relay WebSocket URL on the client. When NEXT_PUBLIC_RELAY_URL is
 * baked in (e.g. the Vercel build points at a public relay) we use it verbatim.
 * Otherwise we derive `ws(s)://<page-host>:8000/ws` from window.location, so a
 * LAN deployment reaches the relay on the same machine that served the page
 * without hard-coding the PC's IP into the bundle.
 */
function resolveRelayUrl(): string {
  if (RELAY_URL) return RELAY_URL
  if (typeof window !== 'undefined' && window.location.hostname) {
    const scheme = window.location.protocol === 'https:' ? 'wss' : 'ws'
    return `${scheme}://${window.location.hostname}:${RELAY_PORT}/ws`
  }
  return DEFAULT_RELAY_URL
}

interface RealtimeContextValue {
  /** Mobile <-> relay socket lifecycle. */
  status: ConnectionStatus
  /** PC presence, driven by `presence` and `auth_ok.pc_online`. */
  pcOnline: boolean
  /** Whether room+token are filled in; null until known (localStorage is client-only). */
  paired: boolean | null
  /** Relay `auth_failed` message while paired (wrong room/token), else null. */
  authError: string | null
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
  const [authError, setAuthError] = useState<string | null>(null)
  // Deliberately null on first render: the server prerender cannot read
  // localStorage, so resolving pairing only after mount avoids a hydration
  // mismatch on the header's "未配对" state.
  const [paired, setPaired] = useState<boolean | null>(DEMO ? true : null)
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
    setAuthError(null)
    // Updating state re-runs the transport effect (deps below) -> reconnect.
    setPairingState(next)
  }, [])

  useEffect(() => {
    setPaired(DEMO || Boolean(pairing.room && pairing.token))
  }, [pairing.room, pairing.token])

  useEffect(() => {
    const transport: RealtimeTransport = DEMO
      ? new MockRealtimeClient()
      : new WsClient(resolveRelayUrl())
    transportRef.current = transport

    const unsubscribe = transport.subscribe((event: RealtimeEvent) => {
      if (event.type === 'status') setStatus(event.status)
      else if (event.type === 'presence' || event.type === 'auth_ok') setPcOnline(event.pcOnline)
      if (event.type === 'auth_ok') setAuthError(null)
      else if (event.type === 'error' && event.code === 'auth_failed')
        setAuthError(event.message || '配对失败')
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
    () => ({
      status,
      pcOnline,
      paired,
      authError,
      demo: DEMO,
      pairing,
      setPairing,
      subscribe,
      send,
    }),
    [status, pcOnline, paired, authError, pairing, setPairing, subscribe, send],
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
