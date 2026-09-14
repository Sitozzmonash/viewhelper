import { answerForText, SCREENSHOT_ANSWER } from './mock-answers'
import type {
  ConnectionStatus,
  HistoryMessage,
  LlmMode,
  OutgoingCommand,
  RealtimeEvent,
  RealtimeListener,
  RealtimeTransport,
  SettingsPayload,
  Speaker,
} from './types'
import { randomId } from './util'

/**
 * Demo transport ("mock socket").
 *
 * Emits exactly the same typed events as `WsClient`, so the whole UI works
 * without a backend. It replays a scripted ASR conversation (partial -> final),
 * streams canned LLM answers with realistic llm_stats/llm_done, pushes a demo
 * screenshot, and answers every outbound command. Selected when
 * NEXT_PUBLIC_DEMO=1 or no relay URL is configured.
 */

const DEFAULT_SETTINGS: SettingsPayload = {
  conversation_prompt:
    '你是一个实时对话助手。根据当前对话和上下文，直接给出用户现在最适合说出的回答。回答简洁、自然，不要解释过程。',
  screenshot_prompt:
    '你是一个截图分析助手。分析截图内容并直接回答最重要的问题。如果截图中包含题目，直接给答案并简要解释。',
  context_messages: 10,
  hotwords: '',
  show_partial: true,
}

const MOCK_TURNS: { speaker: Speaker; text: string }[] = [
  { speaker: 'other', text: '我们这个项目现在的进度怎么样？' },
  { speaker: 'me', text: '目前核心功能已经开发完成，现在在做最后的测试和优化。' },
  { speaker: 'other', text: '好的，那预计什么时候可以上线？' },
  { speaker: 'me', text: '如果测试顺利的话，下周应该可以上线。' },
  { speaker: 'other', text: '上线前需要准备哪些材料？' },
  { speaker: 'me', text: '主要是测试报告、监控看板和一份应急回滚预案。' },
]

/** A lightweight dashboard rendered as an SVG data-url for the demo screenshot. */
const DEMO_PREVIEW =
  'data:image/svg+xml;charset=utf-8,' +
  encodeURIComponent(
    `<svg xmlns="http://www.w3.org/2000/svg" width="1280" height="800" viewBox="0 0 1280 800">
  <rect width="1280" height="800" fill="#f8fafc"/>
  <rect width="1280" height="56" fill="#0f172a"/>
  <circle cx="28" cy="28" r="7" fill="#ff5f57"/><circle cx="52" cy="28" r="7" fill="#febc2e"/><circle cx="76" cy="28" r="7" fill="#28c840"/>
  <text x="104" y="34" fill="#94a3b8" font-family="sans-serif" font-size="18">数据分析 · 控制台</text>
  <rect x="0" y="56" width="220" height="744" fill="#111827"/>
  <rect x="24" y="92" width="172" height="34" rx="8" fill="#2563eb"/>
  <text x="44" y="115" fill="#fff" font-family="sans-serif" font-size="16">数据分析</text>
  <text x="44" y="163" fill="#94a3b8" font-family="sans-serif" font-size="16">用户管理</text>
  <text x="44" y="203" fill="#94a3b8" font-family="sans-serif" font-size="16">产品管理</text>
  <text x="44" y="243" fill="#94a3b8" font-family="sans-serif" font-size="16">系统设置</text>
  <rect x="252" y="88" width="996" height="150" rx="16" fill="#fff" stroke="#e2e8f0"/>
  <text x="284" y="140" fill="#0f172a" font-family="sans-serif" font-size="26" font-weight="bold">2024 上半年运营概览</text>
  <text x="284" y="180" fill="#64748b" font-family="sans-serif" font-size="18">总收入 ¥128,430 · 新增用户 2,843 · 转化率 3.24%</text>
  <rect x="252" y="262" width="996" height="500" rx="16" fill="#fff" stroke="#e2e8f0"/>
  <g fill="#2563eb">
    <rect x="316" y="600" width="86" height="120" rx="6"/>
    <rect x="466" y="540" width="86" height="180" rx="6"/>
    <rect x="616" y="565" width="86" height="155" rx="6"/>
    <rect x="766" y="470" width="86" height="250" rx="6"/>
    <rect x="916" y="420" width="86" height="300" rx="6"/>
    <rect x="1066" y="360" width="86" height="360" rx="6"/>
  </g>
  <polyline points="359,560 509,500 659,520 809,430 959,380 1109,320" fill="none" stroke="#10b981" stroke-width="6"/>
  <g fill="#10b981"><circle cx="359" cy="560" r="8"/><circle cx="509" cy="500" r="8"/><circle cx="659" cy="520" r="8"/><circle cx="809" cy="430" r="8"/><circle cx="959" cy="380" r="8"/><circle cx="1109" cy="320" r="8"/></g>
  <g fill="#94a3b8" font-family="sans-serif" font-size="16" text-anchor="middle">
    <text x="359" y="748">1月</text><text x="509" y="748">2月</text><text x="659" y="748">3月</text>
    <text x="809" y="748">4月</text><text x="959" y="748">5月</text><text x="1109" y="748">6月</text>
  </g>
</svg>`,
  )

interface StreamState {
  chunkTimer: ReturnType<typeof setInterval> | null
  statsTimer: ReturnType<typeof setInterval> | null
  requestId: string
  full: string
  index: number
  startedAt: number
  cancelled: boolean
}

export class MockRealtimeClient implements RealtimeTransport {
  private listeners = new Set<RealtimeListener>()
  private timeouts: ReturnType<typeof setTimeout>[] = []
  private intervals: ReturnType<typeof setInterval>[] = []
  private status: ConnectionStatus = 'disconnected'
  private turnIndex = 0
  private settings: SettingsPayload = { ...DEFAULT_SETTINGS }
  private history: HistoryMessage[] = []
  private textByMessageId = new Map<string, string>()
  private stream: StreamState | null = null

  connect(): void {
    if (this.status === 'connected' || this.status === 'connecting') return
    this.setStatus('connecting')
    this.timeouts.push(
      setTimeout(() => {
        this.setStatus('connected')
        this.emit({ type: 'auth_ok', pcOnline: true })
        this.emit({ type: 'presence', pcOnline: true })
        this.startConversation()
      }, 700),
    )
  }

  disconnect(): void {
    this.timeouts.forEach(clearTimeout)
    this.intervals.forEach(clearInterval)
    this.timeouts = []
    this.intervals = []
    this.teardownStream(false)
    this.setStatus('disconnected')
  }

  subscribe(listener: RealtimeListener): () => void {
    this.listeners.add(listener)
    return () => {
      this.listeners.delete(listener)
    }
  }

  send(command: OutgoingCommand): void {
    switch (command.type) {
      case 'llm_request':
        this.beginAnswer(
          command.payload.request_id,
          command.payload.mode,
          command.payload.message_id ?? null,
        )
        break
      case 'llm_cancel':
        this.teardownStream(true, command.payload.request_id)
        break
      case 'capture_screen':
        this.captureScreen()
        break
      case 'screenshot_delete':
        break // demo: nothing persisted server-side
      case 'conversation_clear':
        this.history = []
        this.textByMessageId.clear()
        this.emit({ type: 'conversation_cleared' })
        break
      case 'screenshot_clear':
        this.emit({ type: 'screenshot_cleared' })
        break
      case 'history_sync_request':
        this.emit({
          type: 'history_sync_response',
          payload: { messages: [...this.history], screenshots: [], ai_answers: [] },
        })
        break
      case 'settings_get':
        this.emit({ type: 'settings_updated', settings: { ...this.settings } })
        break
      case 'settings_update':
        this.settings = { ...this.settings, ...command.payload }
        this.emit({ type: 'settings_updated', settings: { ...this.settings } })
        break
    }
  }

  /* ------------------------------------------------------------- demo flows */

  private emit(event: RealtimeEvent): void {
    this.listeners.forEach((listener) => listener(event))
  }

  private setStatus(status: ConnectionStatus): void {
    if (this.status === status) return
    this.status = status
    this.emit({ type: 'status', status })
  }

  private startConversation(): void {
    const pushTurn = () => {
      const turn = MOCK_TURNS[this.turnIndex % MOCK_TURNS.length]
      this.turnIndex += 1
      this.streamTurn(turn.speaker, turn.text)
    }
    pushTurn()
    this.timeouts.push(setTimeout(pushTurn, 2600))
    this.intervals.push(setInterval(pushTurn, 9000))
  }

  private streamTurn(speaker: Speaker, text: string): void {
    const utteranceId = `u-${randomId()}`
    const showPartial = this.settings.show_partial !== false
    const step = Math.max(2, Math.ceil(text.length / 8))
    let i = 0
    const partial = setInterval(() => {
      i += step
      if (showPartial) {
        this.emit({ type: 'asr_partial', utterance_id: utteranceId, speaker, text: text.slice(0, i) })
      }
      if (i >= text.length) {
        clearInterval(partial)
        this.finalize(utteranceId, speaker, text)
      }
    }, 220)
    this.intervals.push(partial)
  }

  private finalize(utteranceId: string, speaker: Speaker, text: string): void {
    const messageId = `m-${randomId()}`
    const createdAt = Date.now()
    this.history.push({
      message_id: messageId,
      speaker,
      text,
      created_at: createdAt,
      duration_sec: 6,
    })
    this.textByMessageId.set(messageId, text)
    this.emit({
      type: 'asr_final',
      message_id: messageId,
      utterance_id: utteranceId,
      speaker,
      text,
      created_at: createdAt,
      duration_sec: 6,
    })
  }

  private captureScreen(): void {
    const screenshotId = `s-${randomId()}`
    this.emit({
      type: 'screenshot_created',
      screenshot_id: screenshotId,
      preview: DEMO_PREVIEW,
      captured_at: Date.now(),
    })
    // The PC then runs the vision model automatically (mode: screenshot).
    const requestId = `r-${randomId()}`
    this.timeouts.push(setTimeout(() => this.beginAnswer(requestId, 'screenshot', screenshotId), 400))
  }

  private beginAnswer(requestId: string, mode: LlmMode, targetId: string | null): void {
    this.teardownStream(false)
    const full =
      mode === 'screenshot'
        ? SCREENSHOT_ANSWER
        : answerForText(targetId ? (this.textByMessageId.get(targetId) ?? '') : '')
    this.emit({ type: 'llm_started', request_id: requestId, mode })

    const startedAt = Date.now()
    const chunkStep = 3
    const stream: StreamState = {
      chunkTimer: null,
      statsTimer: null,
      requestId,
      full,
      index: 0,
      startedAt,
      cancelled: false,
    }
    this.stream = stream

    stream.chunkTimer = setInterval(() => {
      if (this.stream !== stream || stream.cancelled) return
      const from = stream.index
      stream.index = Math.min(stream.full.length, stream.index + chunkStep)
      if (stream.index > from) {
        this.emit({
          type: 'llm_chunk',
          request_id: requestId,
          delta: stream.full.slice(from, stream.index),
        })
      }
      if (stream.index >= stream.full.length) {
        const elapsedMs = Math.max(1, Date.now() - startedAt)
        const completionTokens = Math.max(1, Math.round(stream.full.length / 2))
        const tps = completionTokens / (elapsedMs / 1000)
        this.emit({
          type: 'llm_done',
          request_id: requestId,
          completion_tokens: completionTokens,
          elapsed_ms: elapsedMs,
          tokens_per_second: Number(tps.toFixed(2)),
        })
        this.teardownStream(false)
      }
    }, 40)
    this.intervals.push(stream.chunkTimer)

    stream.statsTimer = setInterval(() => {
      if (this.stream !== stream || stream.cancelled) return
      const elapsedMs = Math.max(1, Date.now() - startedAt)
      const estimatedTokens = Math.max(1, Math.round(stream.index / 2))
      const tps = estimatedTokens / (elapsedMs / 1000)
      this.emit({
        type: 'llm_stats',
        request_id: requestId,
        tokens_per_second: Number(tps.toFixed(1)),
        elapsed_ms: elapsedMs,
        estimated_tokens: estimatedTokens,
      })
    }, 500)
    this.intervals.push(stream.statsTimer)
  }

  private teardownStream(emitCancelled: boolean, requestId?: string): void {
    const stream = this.stream
    this.stream = null
    if (!stream) return
    stream.cancelled = true
    if (stream.chunkTimer) clearInterval(stream.chunkTimer)
    if (stream.statsTimer) clearInterval(stream.statsTimer)
    if (emitCancelled) {
      this.emit({ type: 'llm_cancelled', request_id: requestId ?? stream.requestId })
    }
  }
}
