'use client'

import { Sparkles } from 'lucide-react'
import { formatClock } from '@/lib/format'
import type { ChatMessage } from '@/lib/realtime/types'
import { cn } from '@/lib/utils'

export function ChatBubble({
  message,
  onAsk,
  isAnswering,
  canAsk,
  disabled,
}: {
  message: ChatMessage
  onAsk: (message: ChatMessage) => void
  isAnswering: boolean
  /** Only final turns (with a persisted message_id) can be asked. */
  canAsk: boolean
  /** Disabled while any request is streaming (single active request). */
  disabled: boolean
}) {
  const isMe = message.speaker === 'me'

  return (
    <div className={cn('flex animate-bubble-in flex-col', isMe ? 'items-end' : 'items-start')}>
      <span className="mb-1 px-1 text-[11px] font-medium text-muted-foreground">
        {isMe ? '我' : '对方'} · {formatClock(message.timestamp)}
      </span>

      <div className={cn('flex max-w-[92%] items-end gap-2', isMe && 'flex-row-reverse')}>
        <div
          className={cn(
            'max-w-[300px] rounded-2xl px-3.5 py-2.5 shadow-[0_1px_2px_rgba(15,23,42,0.04)]',
            isMe ? 'rounded-tr-md bg-bubble-me' : 'rounded-tl-md bg-bubble-other',
          )}
        >
          <p className="text-[15px] leading-relaxed text-foreground text-pretty">
            {message.text}
            {!message.isFinal && (
              <span
                aria-hidden="true"
                className="ml-0.5 inline-block h-[1em] w-[2px] translate-y-[2px] animate-caret-blink bg-primary/70"
              />
            )}
          </p>
        </div>

        {canAsk && (
          <button
            type="button"
            onClick={() => onAsk(message)}
            disabled={disabled}
            aria-label="让 AI 回答这句话"
            title="让 AI 回答"
            className={cn(
              'mb-0.5 flex size-8 shrink-0 items-center justify-center rounded-full border transition-all duration-200 active:scale-90',
              isAnswering
                ? 'border-primary bg-primary text-primary-foreground shadow-sm shadow-primary/30'
                : 'border-primary/20 bg-background text-primary shadow-sm hover:bg-primary/8',
              disabled && 'cursor-not-allowed opacity-40',
            )}
          >
            <Sparkles className="size-3.5" />
          </button>
        )}
      </div>
    </div>
  )
}
