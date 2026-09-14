'use client'

import { Camera, RefreshCw, Trash2 } from 'lucide-react'
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useStreamingAnswer, type AnswerState } from '@/hooks/use-streaming-answer'
import { useTokenRate } from '@/hooks/use-token-rate'
import { useWebSocket } from '@/hooks/use-websocket'
import { formatDateTime } from '@/lib/format'
import { clearScreenshotsCache, loadScreenshots, saveScreenshots } from '@/lib/realtime/cache'
import type { Screenshot } from '@/lib/realtime/types'
import { cn } from '@/lib/utils'
import { AiAnswerCard } from './ai-answer-card'
import { StatusBar } from './status-bar'

export function ScreenshotView() {
  const { send, subscribe, pcOnline, status } = useWebSocket()
  const { answers, isStreaming, stop } = useStreamingAnswer()
  const tokenRate = useTokenRate()
  const [screenshots, setScreenshots] = useState<Screenshot[]>([])
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [activeShotRequestId, setActiveShotRequestId] = useState<string | null>(null)
  const [historyAnswers, setHistoryAnswers] = useState<Record<string, AnswerState>>({})
  const scrollRef = useRef<HTMLDivElement>(null)

  const online = pcOnline && status === 'connected'

  // Hydrate the phone cache once (after mount, to avoid SSR mismatch).
  useEffect(() => {
    const cached = loadScreenshots()
    if (cached.length) {
      setScreenshots(cached)
      setSelectedId((cur) => cur ?? cached[0]?.id ?? null)
    }
  }, [])

  useEffect(
    () =>
      subscribe((event) => {
        switch (event.type) {
          case 'screenshot_created': {
            const shot: Screenshot = {
              id: event.screenshot_id,
              capturedAt: event.captured_at,
              preview: event.preview,
            }
            setActiveShotRequestId(null) // the vision request follows shortly
            setScreenshots((prev) => {
              const next = [shot, ...prev.filter((s) => s.id !== shot.id)]
              saveScreenshots(next)
              return next
            })
            setSelectedId(shot.id)
            break
          }
          case 'llm_started':
            if (event.mode === 'screenshot') setActiveShotRequestId(event.request_id)
            break
          case 'screenshot_cleared':
            clearScreenshotsCache()
            setActiveShotRequestId(null)
            setScreenshots([])
            setSelectedId(null)
            setHistoryAnswers({})
            break
          case 'history_sync_response': {
            const { screenshots: shots, ai_answers } = event.payload
            if (shots.length) {
              setScreenshots((prev) => {
                const map = new Map(prev.map((s) => [s.id, s]))
                for (const hs of shots) {
                  const existing = map.get(hs.screenshot_id)
                  if (!existing) {
                    map.set(hs.screenshot_id, {
                      id: hs.screenshot_id,
                      capturedAt: hs.created_at,
                      preview: hs.preview,
                    })
                  } else if (hs.preview && !existing.preview) {
                    map.set(hs.screenshot_id, { ...existing, preview: hs.preview })
                  }
                }
                const next = [...map.values()].sort((a, b) => b.capturedAt - a.capturedAt)
                saveScreenshots(next)
                return next
              })
            }
            const shotAnswers: Record<string, AnswerState> = {}
            for (const a of ai_answers) {
              if (a.mode !== 'screenshot' || !a.target_id) continue
              shotAnswers[a.target_id] = {
                requestId: a.request_id,
                mode: 'screenshot',
                targetId: a.target_id,
                text: a.answer,
                status: a.status === 'cancelled' ? 'stopped' : a.status === 'error' ? 'error' : 'done',
                tokensPerSecond: a.tokens_per_second,
                timestamp: a.created_at,
              }
            }
            if (Object.keys(shotAnswers).length) {
              setHistoryAnswers((prev) => ({ ...prev, ...shotAnswers }))
            }
            break
          }
        }
      }),
    [subscribe],
  )

  // Keep the selection valid as the list changes.
  useEffect(() => {
    if (screenshots.length === 0) {
      if (selectedId !== null) setSelectedId(null)
      return
    }
    if (!selectedId || !screenshots.some((s) => s.id === selectedId)) {
      setSelectedId(screenshots[0].id)
    }
  }, [screenshots, selectedId])

  const selected = useMemo(
    () => screenshots.find((s) => s.id === selectedId) ?? screenshots[0] ?? null,
    [screenshots, selectedId],
  )
  const isSelectedLatest = selected != null && screenshots[0]?.id === selected.id

  const activeAnswer = activeShotRequestId ? answers[activeShotRequestId] : undefined
  const answer: AnswerState | undefined = isSelectedLatest
    ? (activeAnswer ?? historyAnswers[selected?.id ?? ''])
    : historyAnswers[selected?.id ?? '']

  const recapture = useCallback(() => {
    send({ type: 'capture_screen' })
  }, [send])

  const remove = useCallback(() => {
    if (!selected) return
    send({ type: 'screenshot_delete', payload: { screenshot_id: selected.id } })
    setScreenshots((prev) => {
      const next = prev.filter((s) => s.id !== selected.id)
      saveScreenshots(next)
      return next
    })
    setHistoryAnswers((prev) => {
      const next = { ...prev }
      delete next[selected.id]
      return next
    })
    if (isSelectedLatest) setActiveShotRequestId(null)
    setSelectedId(null)
  }, [selected, send, isSelectedLatest])

  useEffect(() => {
    scrollRef.current?.scrollTo({ top: 0, behavior: 'smooth' })
  }, [selected?.id])

  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <div ref={scrollRef} className="flex-1 space-y-3 overflow-y-auto px-4 py-4">
        {selected ? (
          <>
            <div className="overflow-hidden rounded-2xl border border-border bg-card shadow-sm">
              {selected.preview ? (
                /* eslint-disable-next-line @next/next/no-img-element */
                <img
                  src={selected.preview}
                  alt="PC 截图预览"
                  className="h-auto max-h-[46vh] w-full object-cover object-top"
                />
              ) : (
                <div className="flex aspect-[16/10] w-full items-center justify-center bg-secondary text-xs text-muted-foreground">
                  预览不可用（仅最新截图带预览）
                </div>
              )}
            </div>

            <div className="flex items-center justify-between gap-3 px-0.5">
              <span className="truncate text-[11px] text-muted-foreground">
                {formatDateTime(selected.capturedAt)}
              </span>
              <div className="flex shrink-0 gap-1.5">
                <button
                  type="button"
                  onClick={recapture}
                  disabled={!online}
                  className="flex items-center gap-1.5 rounded-full border border-border bg-background px-3 py-1.5 text-xs font-medium text-foreground/80 active:scale-95 disabled:cursor-not-allowed disabled:opacity-40"
                >
                  <RefreshCw className="size-3.5" />
                  重新截图
                </button>
                <button
                  type="button"
                  onClick={remove}
                  aria-label="删除截图"
                  className="flex size-8 items-center justify-center rounded-full border border-border bg-background text-muted-foreground active:scale-95"
                >
                  <Trash2 className="size-3.5" />
                </button>
              </div>
            </div>

            {answer ? (
              <AiAnswerCard
                text={answer.text}
                timestamp={answer.timestamp}
                isStreaming={answer.status === 'streaming'}
                stopped={answer.status === 'stopped'}
                error={answer.status === 'error' ? (answer.errorMessage ?? '生成失败') : undefined}
                onStop={stop}
              />
            ) : (
              <div className="rounded-2xl border border-dashed border-border px-4 py-6 text-center text-xs text-muted-foreground">
                {isSelectedLatest
                  ? online
                    ? '正在等待 AI 分析截图…'
                    : 'PC 连接后会自动分析截图'
                  : '该历史截图暂无回答'}
              </div>
            )}

            {screenshots.length > 1 && (
              <div className="pt-1">
                <div className="mb-2 text-[11px] font-medium text-muted-foreground">历史截图</div>
                <div className="flex gap-2 overflow-x-auto pb-1">
                  {screenshots.map((s) => (
                    <button
                      key={s.id}
                      type="button"
                      onClick={() => setSelectedId(s.id)}
                      aria-label={`查看 ${formatDateTime(s.capturedAt)} 的截图`}
                      className={cn(
                        'relative h-14 w-24 shrink-0 overflow-hidden rounded-xl border transition',
                        s.id === selected.id
                          ? 'border-primary ring-2 ring-primary/20'
                          : 'border-border opacity-80',
                      )}
                    >
                      {s.preview ? (
                        /* eslint-disable-next-line @next/next/no-img-element */
                        <img src={s.preview} alt="" className="h-full w-full object-cover" />
                      ) : (
                        <span className="flex h-full w-full items-center justify-center bg-secondary text-[9px] text-muted-foreground">
                          无预览
                        </span>
                      )}
                    </button>
                  ))}
                </div>
              </div>
            )}
          </>
        ) : (
          <div className="flex min-h-[420px] flex-col items-center justify-center px-8 text-center">
            <span className="flex size-14 items-center justify-center rounded-2xl bg-primary/10 text-primary">
              <Camera className="size-6" />
            </span>
            <h3 className="mt-4 text-base font-semibold">等待截图</h3>
            <p className="mt-1.5 text-sm leading-relaxed text-muted-foreground">
              在 PC 按 <span className="font-mono text-foreground/80">Ctrl + Shift + Space</span>
              ，截图会自动发送给 AI 并在这里流式显示回答。
            </p>
            <button
              type="button"
              onClick={recapture}
              disabled={!online}
              className="mt-5 rounded-2xl bg-primary px-5 py-2.5 text-sm font-semibold text-primary-foreground active:scale-95 disabled:cursor-not-allowed disabled:opacity-40"
            >
              让 PC 截图
            </button>
          </div>
        )}
      </div>

      <StatusBar
        items={
          online
            ? [{ label: isStreaming ? 'AI 回答中' : 'LLM 已就绪', ok: true }]
            : [
                { label: 'PC 离线', ok: false },
                { label: '截图不可用', ok: false },
              ]
        }
        tokenRate={answer ? tokenRate : null}
      />
    </div>
  )
}
