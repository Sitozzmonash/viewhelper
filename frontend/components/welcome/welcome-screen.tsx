'use client'

import { ArrowRight, Sparkles } from 'lucide-react'
import Link from 'next/link'
import { PhoneShell } from '@/components/phone-shell'
import { Waveform } from '@/components/waveform'
import { useWebSocket } from '@/hooks/use-websocket'
import { cn } from '@/lib/utils'

export function WelcomeScreen() {
  const { status, pcOnline } = useWebSocket()

  const label = pcOnline
    ? 'PC 已连接'
    : status === 'connecting'
      ? '正在连接 PC…'
      : 'PC 未连接'

  return (
    <PhoneShell>
      <div className="flex flex-1 flex-col items-center justify-center px-8 text-center">
        <div className="relative flex size-32 items-center justify-center">
          <span className="absolute size-20 rounded-full bg-primary/10 animate-ring-out" />
          <span className="absolute size-20 rounded-full bg-primary/10 animate-ring-out [animation-delay:0.9s]" />
          <div className="relative flex size-20 items-center justify-center rounded-[1.75rem] bg-primary shadow-lg shadow-primary/25">
            <Sparkles className="size-9 text-primary-foreground" />
          </div>
        </div>

        <Waveform bars={7} className="mt-9 h-5 text-primary" />

        <h1 className="mt-6 text-3xl font-bold tracking-tight text-foreground text-balance">
          AI 实时助手
        </h1>
        <p className="mt-2.5 text-sm text-muted-foreground">实时对话 · 截图问答 · AI 辅助</p>

        <div className="mt-7 flex items-center gap-2 rounded-full border border-border bg-secondary/60 px-3.5 py-1.5 text-xs font-medium text-muted-foreground">
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

      <div className="px-6 pb-8">
        <Link
          href="/assistant"
          className="flex w-full items-center justify-center gap-2 rounded-2xl bg-primary py-4 text-base font-semibold text-primary-foreground shadow-lg shadow-primary/25 transition-transform active:scale-[0.98]"
        >
          进入助手
          <ArrowRight className="size-4.5" />
        </Link>
      </div>
    </PhoneShell>
  )
}
