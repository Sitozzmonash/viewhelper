'use client'

import { Check, Copy, Sparkles, Square } from 'lucide-react'
import { useCallback, useEffect, useState } from 'react'
import { formatClock } from '@/lib/format'
import { cn } from '@/lib/utils'
import { Markdown } from './markdown'

export function AiAnswerCard({
  text,
  timestamp,
  isStreaming,
  onStop,
  stopped = false,
  error,
}: {
  text: string
  timestamp: number
  isStreaming: boolean
  onStop: () => void
  stopped?: boolean
  /** Error message when generation failed (PRD §10.4). */
  error?: string | null
}) {
  const [copied, setCopied] = useState(false)

  useEffect(() => {
    if (!copied) return
    const timer = setTimeout(() => setCopied(false), 1600)
    return () => clearTimeout(timer)
  }, [copied])

  const copy = useCallback(async () => {
    try {
      await navigator.clipboard.writeText(text)
      setCopied(true)
    } catch {
      setCopied(false)
    }
  }, [text])

  const showBody = text.trim().length > 0

  return (
    <div className="animate-fade-up">
      <div className="rounded-2xl border border-ai-border bg-ai-surface p-3.5">
        <div className="flex items-center gap-2">
          <span className="flex size-7 shrink-0 items-center justify-center rounded-full bg-ai text-primary-foreground">
            <Sparkles className="size-3.5" />
          </span>
          <span className="text-sm font-semibold text-ai-foreground">AI 回答</span>
          <span className="text-[11px] text-muted-foreground">{formatClock(timestamp)}</span>
          {stopped && (
            <span className="rounded-full bg-muted px-2 py-0.5 text-[10px] text-muted-foreground">
              已停止
            </span>
          )}
          {error && (
            <span className="rounded-full bg-destructive/12 px-2 py-0.5 text-[10px] text-destructive">
              出错
            </span>
          )}

          {isStreaming && (
            <button
              type="button"
              onClick={onStop}
              aria-label="停止回答"
              className="ml-auto flex size-7 items-center justify-center rounded-lg text-destructive transition-colors hover:bg-destructive/10 active:scale-90"
            >
              <Square className="size-3.5 fill-current" />
            </button>
          )}

          <button
            type="button"
            onClick={copy}
            aria-label="复制回答"
            className={cn(
              'flex size-7 items-center justify-center rounded-lg text-muted-foreground transition-colors hover:bg-ai/10 hover:text-ai-foreground active:scale-90',
              !isStreaming && 'ml-auto',
            )}
          >
            {copied ? <Check className="size-3.5" /> : <Copy className="size-3.5" />}
          </button>
        </div>

        {error ? (
          <div className="mt-2.5 rounded-xl bg-destructive/8 px-3 py-2 text-[13px] leading-relaxed text-destructive">
            {error}
          </div>
        ) : showBody ? (
          <div className={cn('mt-2.5', isStreaming && 'streaming-caret')}>
            <Markdown>{text}</Markdown>
          </div>
        ) : isStreaming ? (
          <div className="mt-2.5 flex items-center gap-1.5 text-[13px] text-ai-foreground/70">
            <span className="size-1.5 animate-status-pulse rounded-full bg-ai" />
            正在生成回答…
          </div>
        ) : null}
      </div>

      {isStreaming && (
        <div className="mt-2.5 flex justify-center">
          <button
            type="button"
            onClick={onStop}
            className="flex items-center gap-1.5 rounded-full border border-destructive/40 bg-background px-4 py-1.5 text-sm font-medium text-destructive transition-transform active:scale-95"
          >
            <Square className="size-3 fill-current" />
            停止回答
          </button>
        </div>
      )}
    </div>
  )
}
