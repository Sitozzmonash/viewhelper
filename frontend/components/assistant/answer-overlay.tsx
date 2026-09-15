'use client'

import type { AnswerState } from '@/hooks/use-streaming-answer'
import { AiAnswerCard } from './ai-answer-card'

/**
 * Enlarged answer overlay (Tasks B & C).
 *
 * Fills most of the view with the AI answer so it can be read comfortably.
 * Tapping anywhere (backdrop or panel) collapses it back; the card's own
 * stop/copy/expand buttons stop propagation so they keep working.
 */
export function AnswerOverlay({
  answer,
  onClose,
  onStop,
}: {
  answer: AnswerState
  onClose: () => void
  onStop: () => void
}) {
  return (
    <div
      className="absolute inset-0 z-40 flex items-center justify-center bg-black/45 p-2 backdrop-blur-[2px]"
      onClick={onClose}
      role="presentation"
    >
      <div className="flex h-[94%] w-full max-w-[720px] flex-col" onClick={onClose} role="presentation">
        <AiAnswerCard
          text={answer.text}
          timestamp={answer.timestamp}
          isStreaming={answer.status === 'streaming'}
          stopped={answer.status === 'stopped'}
          error={answer.status === 'error' ? (answer.errorMessage ?? '生成失败') : undefined}
          onStop={onStop}
          expanded
          fill
          onToggleExpand={onClose}
        />
        <p className="mt-2 text-center text-[11px] text-muted-foreground">点击任意处收起</p>
      </div>
    </div>
  )
}
