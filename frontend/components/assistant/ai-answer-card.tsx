'use client'

import { Check, Copy, Maximize2, Minimize2, Sparkles, Square } from 'lucide-react'
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
  onToggleExpand,
  expanded = false,
  fill = false,
}: {
  text: string
  timestamp: number
  isStreaming: boolean
  onStop: () => void
  stopped?: boolean
  /** Error message when generation failed (PRD §10.4). */
  error?: string | null
  /** When set, the whole card is clickable to expand/collapse the answer. */
  onToggleExpand?: () => void
  /** Whether the card is currently shown in the enlarged overlay. */
  expanded?: boolean
  /** Fill the overlay panel height and scroll the body inside it. */
  fill?: boolean
}) {
  const [copied, setCopied] = useState(false)

  useEffect(() => {
    if (!copied) return
    const timer = setTimeout(() => setCopied(false), 1600)
    return () => clearTimeout(timer)
  }, [copied])

  const copy = useCallback(
    async (event: React.MouseEvent) => {
      event.stopPropagation()
      try {
        await navigator.clipboard.writeText(text)
        setCopied(true)
      } catch {
        setCopied(false)
      }
    },
    [text],
  )

  const showBody = text.trim().length > 0
  const interactive = Boolean(onToggleExpand)

  return (
    <div className={cn('animate-fade-up', fill && 'flex h-full min-h-0 flex-col')}>
      <div
        onClick={interactive ? onToggleExpand : undefined}
        role={interactive ? 'button' : undefined}
        tabIndex={interactive ? 0 : undefined}
        onKeyDown={
          interactive
            ? (e) => {
                if (e.key === 'Enter' || e.key === ' ') {
                  e.preventDefault()
                  onToggleExpand?.()
                }
              }
            : undefined
        }
        className={cn(
          'rounded-2xl border border-ai-border bg-ai-surface p-3.5',
          fill && 'flex min-h-0 flex-1 flex-col',
          interactive && 'cursor-pointer transition-colors hover:border-ai/40',
        )}
      >
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

          <div className="ml-auto flex items-center gap-0.5">
            {isStreaming && (
              <button
                type="button"
                onClick={(e) => {
                  e.stopPropagation()
                  onStop()
                }}
                aria-label="停止回答"
                className="flex size-7 items-center justify-center rounded-lg text-destructive transition-colors hover:bg-destructive/10 active:scale-90"
              >
                <Square className="size-3.5 fill-current" />
              </button>
            )}

            {interactive && (
              <button
                type="button"
                onClick={(e) => {
                  e.stopPropagation()
                  onToggleExpand?.()
                }}
                aria-label={expanded ? '收起回答' : '放大回答'}
                className="flex size-7 items-center justify-center rounded-lg text-muted-foreground transition-colors hover:bg-ai/10 hover:text-ai-foreground active:scale-90"
              >
                {expanded ? <Minimize2 className="size-3.5" /> : <Maximize2 className="size-3.5" />}
              </button>
            )}

            <button
              type="button"
              onClick={copy}
              aria-label="复制回答"
              className="flex size-7 items-center justify-center rounded-lg text-muted-foreground transition-colors hover:bg-ai/10 hover:text-ai-foreground active:scale-90"
            >
              {copied ? <Check className="size-3.5" /> : <Copy className="size-3.5" />}
            </button>
          </div>
        </div>

        {error ? (
          <div className="mt-2.5 rounded-xl bg-destructive/8 px-3 py-2 text-[13px] leading-relaxed text-destructive">
            {error}
          </div>
        ) : showBody ? (
          <div
            className={cn(
              'mt-2.5',
              isStreaming && 'streaming-caret',
              fill && 'min-h-0 flex-1 overflow-y-auto',
            )}
          >
            <Markdown>{text}</Markdown>
          </div>
        ) : isStreaming ? (
          <div className="mt-2.5 flex items-center gap-1.5 text-[13px] text-ai-foreground/70">
            <span className="size-1.5 animate-status-pulse rounded-full bg-ai" />
            正在生成回答…
          </div>
        ) : null}
      </div>

      {isStreaming && !fill && (
        <div className="mt-2.5 flex justify-center">
          <button
            type="button"
            onClick={(e) => {
              e.stopPropagation()
              onStop()
            }}
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
