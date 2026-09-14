'use client'

import { Settings } from 'lucide-react'
import { useWebSocket } from '@/hooks/use-websocket'
import { cn } from '@/lib/utils'

export function AssistantHeader({ onOpenSettings }: { onOpenSettings: () => void }) {
  const { status, pcOnline } = useWebSocket()
  // Presence (relay -> mobile) is the source of truth for "PC 在线" (PRD §20).
  const label = pcOnline ? 'PC 在线' : status === 'connected' ? 'PC 离线' : '连接中'

  return (
    <header className="flex items-center justify-between px-5 pt-5 pb-3">
      <div>
        <h1 className="text-[22px] font-bold tracking-tight text-foreground">AI 助手</h1>
        <div className="mt-1 flex items-center gap-1.5 text-xs text-muted-foreground">
          <span className="relative flex size-2">
            {pcOnline && <span className="absolute inset-0 rounded-full bg-success animate-ring-out" />}
            <span
              className={cn(
                'relative size-2 rounded-full',
                pcOnline ? 'bg-success' : 'bg-muted-foreground/40',
              )}
            />
          </span>
          {label}
        </div>
      </div>

      <button
        type="button"
        onClick={onOpenSettings}
        aria-label="设置"
        className="flex size-9 items-center justify-center rounded-full border border-border bg-background text-foreground/70 transition-all hover:bg-secondary active:scale-90"
      >
        <Settings className="size-4.5" />
      </button>
    </header>
  )
}
