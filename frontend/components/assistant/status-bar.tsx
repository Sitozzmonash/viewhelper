'use client'

import { cn } from '@/lib/utils'

export interface StatusItem {
  label: string
  ok: boolean
}

export function StatusBar({
  items,
  tokenRate,
}: {
  items: StatusItem[]
  tokenRate?: number | null
}) {
  return (
    <div className="flex min-h-10 items-center justify-between gap-3 border-t border-border bg-background/95 px-5 py-2.5 text-[11px] text-muted-foreground backdrop-blur">
      <div className="flex min-w-0 items-center gap-3">
        {items.map((item) => (
          <span key={item.label} className="flex min-w-0 items-center gap-1.5 whitespace-nowrap">
            <span
              className={cn(
                'size-1.5 shrink-0 rounded-full',
                item.ok ? 'bg-success' : 'bg-muted-foreground/35',
              )}
            />
            {item.label}
          </span>
        ))}
      </div>

      <span className="shrink-0 font-mono tabular-nums">
        {tokenRate == null ? '-- token/s' : `${tokenRate.toFixed(1)} token/s`}
      </span>
    </div>
  )
}
