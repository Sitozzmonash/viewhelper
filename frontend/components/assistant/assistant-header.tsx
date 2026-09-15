'use client'

import { Settings } from 'lucide-react'
import { useWebSocket } from '@/hooks/use-websocket'
import { cn } from '@/lib/utils'

export function AssistantHeader({ onOpenSettings }: { onOpenSettings: () => void }) {
  const { status, pcOnline, paired, authError } = useWebSocket()

  // An unpaired or rejected client would otherwise spin on "连接中" forever with
  // no clue why, so surface those as actionable states that open settings.
  // `paired` is null until the client knows (localStorage is unavailable during
  // prerender), so only a definite false counts as unpaired.
  const needsPairing = paired === false
  const needsFix = paired === true && authError !== null
  const actionable = needsPairing || needsFix

  const label = needsPairing
    ? '未配对 · 点此设置'
    : needsFix
      ? '配对失败 · 点此检查'
      : pcOnline
        ? 'PC 在线'
        : status === 'connected'
          ? 'PC 离线'
          : '连接中'

  const dotClass = actionable
    ? 'bg-amber-500'
    : pcOnline
      ? 'bg-success'
      : 'bg-muted-foreground/40'

  const dot = (
    <span className="relative flex size-2">
      {pcOnline && !actionable && (
        <span className="absolute inset-0 rounded-full bg-success animate-ring-out" />
      )}
      <span className={cn('relative size-2 rounded-full', dotClass)} />
    </span>
  )

  return (
    <header className="flex items-center justify-between px-5 pt-5 pb-3">
      <div>
        <h1 className="text-[22px] font-bold tracking-tight text-foreground">AI 助手</h1>
        {actionable ? (
          <button
            type="button"
            onClick={onOpenSettings}
            className="mt-1 flex items-center gap-1.5 text-xs font-medium text-amber-600 transition-opacity hover:opacity-80 active:opacity-60"
          >
            {dot}
            {label}
          </button>
        ) : (
          <div className="mt-1 flex items-center gap-1.5 text-xs text-muted-foreground">
            {dot}
            {label}
          </div>
        )}
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
