'use client'

import { MessageSquareText } from 'lucide-react'
import { Fragment, useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { Waveform } from '@/components/waveform'
import { useRealtimeConversation } from '@/hooks/use-realtime-conversation'
import { useStreamingAnswer, type AnswerState } from '@/hooks/use-streaming-answer'
import { useTokenRate } from '@/hooks/use-token-rate'
import { useWebSocket } from '@/hooks/use-websocket'
import type { ChatMessage } from '@/lib/realtime/types'
import { AiAnswerCard } from './ai-answer-card'
import { AnswerOverlay } from './answer-overlay'
import { ChatBubble } from './chat-bubble'
import { StatusBar } from './status-bar'

export function ConversationView() {
  const { messages, isListening } = useRealtimeConversation()
  const { pcOnline, status } = useWebSocket()
  const { answers, isStreaming, start, stop } = useStreamingAnswer()
  const tokenRate = useTokenRate()
  const scrollRef = useRef<HTMLDivElement>(null)
  const [expanded, setExpanded] = useState(false)

  const online = pcOnline && status === 'connected'

  // Newest answer per target message_id, used only to light up a bubble's ask dot.
  const latestByTarget = useMemo(() => {
    const map: Record<string, AnswerState> = {}
    for (const a of Object.values(answers)) {
      if (!a.targetId || a.mode !== 'conversation') continue
      const prev = map[a.targetId]
      if (!prev || a.timestamp >= prev.timestamp) map[a.targetId] = a
    }
    return map
  }, [answers])

  // The single answer shown in the docked bottom box: the most recent
  // conversation answer, whether it was asked from the phone or fired by the
  // PC-side Shift+Ctrl+Enter hotkey.
  const latestAnswer = useMemo(() => {
    let best: AnswerState | undefined
    for (const a of Object.values(answers)) {
      if (a.mode !== 'conversation') continue
      if (!best || a.timestamp >= best.timestamp) best = a
    }
    return best
  }, [answers])

  const handleAsk = useCallback(
    (message: ChatMessage) => {
      if (isStreaming) return // single active request
      if (!message.isFinal || !message.messageId) return
      start('conversation', message.messageId)
    },
    [isStreaming, start],
  )

  const lastText = messages.length ? messages[messages.length - 1].text : ''

  // Always pin the transcript to the newest content: every messages update —
  // including rapid streaming text updates — must land at the bottom on its own.
  // Instant ('auto') scrolling keeps streaming updates from stuttering.
  useEffect(() => {
    const el = scrollRef.current
    if (!el) return
    el.scrollTo({ top: el.scrollHeight, behavior: 'auto' })
  }, [messages.length, lastText])

  const hasAnyAnswer = Boolean(latestAnswer)

  return (
    <div className="relative flex min-h-0 flex-1 flex-col">
      <div ref={scrollRef} className="flex-1 space-y-4 overflow-y-auto px-4 py-4">
        {messages.length === 0 ? (
          <div className="flex h-full min-h-56 flex-col items-center justify-center px-8 text-center text-muted-foreground">
            <span className="flex size-12 items-center justify-center rounded-2xl bg-secondary">
              <MessageSquareText className="size-5" />
            </span>
            <p className="mt-3 text-sm font-medium text-foreground/75">
              {online ? '等待新的对话…' : 'PC 连接后，对话会显示在这里'}
            </p>
            <p className="mt-1 text-xs">麦克风显示“我”，系统声音显示“对方”</p>
          </div>
        ) : (
          messages.map((message) => {
            const answer = message.messageId ? latestByTarget[message.messageId] : undefined
            const canAsk = message.isFinal && Boolean(message.messageId)
            return (
              <Fragment key={message.id}>
                <ChatBubble
                  message={message}
                  onAsk={handleAsk}
                  isAnswering={answer?.status === 'streaming'}
                  canAsk={canAsk}
                  disabled={isStreaming}
                />
              </Fragment>
            )
          })
        )}
      </div>

      {/* Docked AI-answer box: every answer is concentrated here. Tap to enlarge. */}
      {latestAnswer ? (
        <div className="border-t border-border bg-background/95 px-4 py-3 backdrop-blur">
          <div className="max-h-[38vh] overflow-y-auto">
            <AiAnswerCard
              text={latestAnswer.text}
              timestamp={latestAnswer.timestamp}
              isStreaming={latestAnswer.status === 'streaming'}
              stopped={latestAnswer.status === 'stopped'}
              error={
                latestAnswer.status === 'error'
                  ? (latestAnswer.errorMessage ?? '生成失败')
                  : undefined
              }
              onStop={stop}
              onToggleExpand={() => setExpanded(true)}
            />
          </div>
        </div>
      ) : null}

      {/* Listening indicator row (PRD §16.3). */}
      <div className="flex items-center gap-2 border-t border-border bg-background/95 px-5 py-2 text-[11px] text-muted-foreground backdrop-blur">
        {online ? (
          <>
            <Waveform bars={4} className="h-3.5 text-primary" />
            <span>{isStreaming ? 'AI 回答中…' : '正在监听对话…'}</span>
          </>
        ) : (
          <span className="flex items-center gap-1.5">
            <span className="size-1.5 rounded-full bg-muted-foreground/40" />
            PC 离线 · ASR 不可用
          </span>
        )}
      </div>

      <StatusBar
        items={
          online
            ? [
                { label: 'ASR 正常', ok: isListening },
                { label: 'LLM 已就绪', ok: true },
              ]
            : [
                { label: 'PC 离线', ok: false },
                { label: 'ASR 不可用', ok: false },
              ]
        }
        tokenRate={isStreaming || hasAnyAnswer ? tokenRate : null}
      />

      {expanded && latestAnswer ? (
        <AnswerOverlay
          answer={latestAnswer}
          onClose={() => setExpanded(false)}
          onStop={stop}
        />
      ) : null}
    </div>
  )
}
