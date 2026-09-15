'use client'

import { Check, Link2, Trash2, X } from 'lucide-react'
import { useEffect, useState } from 'react'
import { useWebSocket } from '@/hooks/use-websocket'
import type { SettingsPayload } from '@/lib/realtime/types'
import { cn } from '@/lib/utils'

/**
 * Settings sheet (PRD §8.3, §18, §22).
 *
 * - Loads PC settings via settings_get, edits them, and pushes settings_update;
 *   reflects settings_updated.
 * - Pairing: room + relay token saved to localStorage; reconnects on change.
 * - Clear conversation / screenshot history with a two-step confirm dialog.
 * - LLM API keys are never shown, asked for, or transmitted.
 */

interface SettingsForm {
  conversation_prompt: string
  screenshot_prompt: string
  context_messages: number
  hotwords: string
  show_partial: boolean
}

const DEFAULT_FORM: SettingsForm = {
  conversation_prompt:
    '你是一个实时对话助手。根据当前对话和上下文，直接给出用户现在最适合说出的回答。回答简洁、自然，不要解释过程。',
  screenshot_prompt:
    '你是一个截图分析助手。分析截图内容并直接回答最重要的问题。如果截图中包含题目，直接给答案并简要解释。',
  context_messages: 10,
  hotwords: '',
  show_partial: true,
}

const inputClass =
  'mt-1.5 w-full rounded-2xl border border-border bg-secondary/45 px-3.5 py-2.5 text-sm outline-none transition focus:border-primary/45 focus:ring-2 focus:ring-primary/10 disabled:opacity-50'

export function SettingsPanel({ open, onClose }: { open: boolean; onClose: () => void }) {
  const { send, subscribe, pairing, setPairing, status, demo } = useWebSocket()
  const [form, setForm] = useState<SettingsForm>(DEFAULT_FORM)
  const [saved, setSaved] = useState(false)
  const [room, setRoom] = useState(pairing.room)
  const [token, setToken] = useState(pairing.token)
  const [pairSaved, setPairSaved] = useState(false)
  const [confirm, setConfirm] = useState(false)

  // Reflect settings_updated (response to settings_get, or ack of settings_update).
  useEffect(
    () =>
      subscribe((event) => {
        if (event.type !== 'settings_updated') return
        const s: SettingsPayload = event.settings
        setForm((prev) => ({
          conversation_prompt: s.conversation_prompt ?? prev.conversation_prompt,
          screenshot_prompt: s.screenshot_prompt ?? prev.screenshot_prompt,
          context_messages: s.context_messages ?? prev.context_messages,
          hotwords: s.hotwords ?? prev.hotwords,
          show_partial: s.show_partial ?? prev.show_partial,
        }))
      }),
    [subscribe],
  )

  // Pull the current settings from the PC whenever the sheet opens.
  useEffect(() => {
    if (open) send({ type: 'settings_get' })
  }, [open, send])

  // Keep the pairing inputs in sync with the stored config.
  useEffect(() => {
    setRoom(pairing.room)
    setToken(pairing.token)
  }, [pairing])

  if (!open) return null

  const update = <K extends keyof SettingsForm>(key: K, value: SettingsForm[K]) =>
    setForm((prev) => ({ ...prev, [key]: value }))

  const saveSettings = () => {
    send({ type: 'settings_update', payload: { ...form } })
    setSaved(true)
    window.setTimeout(() => setSaved(false), 1200)
  }

  const savePairing = () => {
    setPairing({ room, token })
    setPairSaved(true)
    window.setTimeout(() => setPairSaved(false), 1500)
  }

  const confirmClear = () => {
    send({ type: 'conversation_clear' })
    send({ type: 'screenshot_clear' })
    setConfirm(false)
  }

  return (
    <div className="absolute inset-0 z-50 flex flex-col bg-black/18 backdrop-blur-[2px]">
      <button className="min-h-14 flex-1" aria-label="关闭设置" onClick={onClose} />
      <section className="max-h-[88%] rounded-t-[1.75rem] border-t border-border bg-background shadow-[0_-20px_60px_rgba(15,23,42,0.12)]">
        <div className="flex items-center justify-between border-b border-border px-5 py-4">
          <div>
            <h2 className="text-lg font-semibold">设置</h2>
            <p className="mt-0.5 text-xs text-muted-foreground">Prompt 与配对信息会同步到 PC 端使用</p>
          </div>
          <button
            type="button"
            onClick={onClose}
            className="flex size-9 items-center justify-center rounded-full bg-secondary text-muted-foreground active:scale-90"
            aria-label="关闭"
          >
            <X className="size-4.5" />
          </button>
        </div>

        <div className="max-h-[calc(88dvh-72px)] space-y-6 overflow-y-auto px-5 py-5">
          {/* ---------------------------------------------------- pairing */}
          <div className="space-y-3">
            <div className="flex items-center gap-2">
              <Link2 className="size-4 text-primary" />
              <h3 className="text-sm font-semibold">配对</h3>
              {demo && (
                <span className="rounded-full bg-secondary px-2 py-0.5 text-[10px] text-muted-foreground">
                  演示模式
                </span>
              )}
            </div>
            <label className="block">
              <span className="text-xs font-medium text-muted-foreground">房间号 Room</span>
              <input
                value={room}
                onChange={(e) => setRoom(e.target.value)}
                placeholder="例如 my-room"
                disabled={demo}
                autoCapitalize="none"
                autoCorrect="off"
                className={inputClass}
              />
            </label>
            <label className="block">
              <span className="text-xs font-medium text-muted-foreground">
                令牌 Token（Relay Secret）
              </span>
              <input
                value={token}
                onChange={(e) => setToken(e.target.value)}
                type="password"
                placeholder="与 PC 端 RELAY_SECRET 一致"
                disabled={demo}
                autoCapitalize="none"
                autoCorrect="off"
                className={inputClass}
              />
            </label>
            <button
              type="button"
              onClick={savePairing}
              disabled={demo}
              className="flex w-full items-center justify-center gap-2 rounded-2xl border border-primary/30 bg-primary/5 py-3 text-sm font-semibold text-primary active:scale-[0.99] disabled:opacity-40"
            >
              {pairSaved ? <Check className="size-4" /> : null}
              {pairSaved ? '已保存并重连' : '保存并连接'}
            </button>
            <p className="text-[11px] leading-relaxed text-muted-foreground">
              仅保存房间号与连接令牌，用于和 PC 进入同一房间。
              {demo ? '演示模式无需配对。' : status === 'connected' ? '当前已连接到 Relay。' : '当前未连接到 Relay。'}
            </p>
          </div>

          <div className="h-px bg-border" />

          {/* ---------------------------------------------------- prompts */}
          <label className="block">
            <span className="text-sm font-semibold">对话回答 Prompt</span>
            <textarea
              value={form.conversation_prompt}
              onChange={(e) => update('conversation_prompt', e.target.value)}
              rows={4}
              className="mt-2 w-full resize-none rounded-2xl border border-border bg-secondary/45 px-3.5 py-3 text-sm leading-relaxed outline-none transition focus:border-primary/45 focus:ring-2 focus:ring-primary/10"
            />
          </label>

          <label className="block">
            <span className="text-sm font-semibold">截图回答 Prompt</span>
            <textarea
              value={form.screenshot_prompt}
              onChange={(e) => update('screenshot_prompt', e.target.value)}
              rows={4}
              className="mt-2 w-full resize-none rounded-2xl border border-border bg-secondary/45 px-3.5 py-3 text-sm leading-relaxed outline-none transition focus:border-primary/45 focus:ring-2 focus:ring-primary/10"
            />
          </label>

          <label className="flex items-center justify-between rounded-2xl border border-border px-4 py-3.5">
            <div>
              <div className="text-sm font-semibold">对话上下文</div>
              <div className="mt-0.5 text-xs text-muted-foreground">点击气泡时携带最近几条对话</div>
            </div>
            <select
              value={form.context_messages}
              onChange={(e) => update('context_messages', Number(e.target.value))}
              className="rounded-xl border border-border bg-background px-3 py-2 text-sm font-medium outline-none"
            >
              {[4, 6, 8, 10, 15, 20].map((count) => (
                <option key={count} value={count}>
                  {count} 条
                </option>
              ))}
            </select>
          </label>

          <label className="block">
            <span className="text-sm font-semibold">ASR 热词</span>
            <span className="mt-0.5 block text-xs text-muted-foreground">
              用空格分隔，提升专有名词识别准确率
            </span>
            <input
              value={form.hotwords}
              onChange={(e) => update('hotwords', e.target.value)}
              placeholder="例如 项目名 人名 术语"
              className="mt-2 w-full rounded-2xl border border-border bg-secondary/45 px-3.5 py-2.5 text-sm outline-none transition focus:border-primary/45 focus:ring-2 focus:ring-primary/10"
            />
          </label>

          <div className="flex items-center justify-between rounded-2xl border border-border px-4 py-3.5">
            <div>
              <div className="text-sm font-semibold">显示实时 Partial</div>
              <div className="mt-0.5 text-xs text-muted-foreground">识别过程中先显示草稿文字</div>
            </div>
            <button
              type="button"
              role="switch"
              aria-checked={form.show_partial}
              aria-label="显示实时 Partial"
              onClick={() => update('show_partial', !form.show_partial)}
              className={cn(
                'relative h-6 w-11 shrink-0 rounded-full transition',
                form.show_partial ? 'bg-primary' : 'bg-muted-foreground/30',
              )}
            >
              <span
                className={cn(
                  'absolute top-0.5 size-5 rounded-full bg-white shadow transition-all',
                  form.show_partial ? 'left-[22px]' : 'left-0.5',
                )}
              />
            </button>
          </div>

          <div className="rounded-2xl bg-secondary/55 px-4 py-3 text-xs leading-relaxed text-muted-foreground">
            LLM API 地址、Key 和模型仅在 PC 端配置，手机端不查看、不保存、不传输任何密钥。
          </div>

          {/* ---------------------------------------------------- clear */}
          <div className="space-y-2">
            <button
              type="button"
              onClick={() => setConfirm(true)}
              className="flex w-full items-center justify-between rounded-2xl border border-border px-4 py-3.5 text-sm font-medium active:scale-[0.99]"
            >
              <span className="flex items-center gap-2">
                <Trash2 className="size-4 text-destructive" />
                一键清空对话与截图历史
              </span>
              <span className="text-xs text-muted-foreground">不可恢复</span>
            </button>
          </div>

          <button
            type="button"
            onClick={saveSettings}
            className="flex w-full items-center justify-center gap-2 rounded-2xl bg-primary py-3.5 text-sm font-semibold text-primary-foreground shadow-sm shadow-primary/20 active:scale-[0.99]"
          >
            {saved ? <Check className="size-4" /> : null}
            {saved ? '已保存' : '保存设置'}
          </button>
        </div>
      </section>

      {/* Two-step confirm dialog (PRD §18). */}
      {confirm && (
        <div className="absolute inset-0 z-[60] flex items-center justify-center bg-black/40 px-8">
          <div className="w-full max-w-xs rounded-3xl bg-background p-5 shadow-xl">
            <h3 className="text-base font-semibold text-foreground">
              确定清空所有对话记录和截图历史？
            </h3>
            <p className="mt-2 text-sm text-muted-foreground">此操作不可恢复。</p>
            <div className="mt-5 flex gap-2">
              <button
                type="button"
                onClick={() => setConfirm(false)}
                className="flex-1 rounded-2xl border border-border py-2.5 text-sm font-medium text-foreground/80 active:scale-[0.98]"
              >
                取消
              </button>
              <button
                type="button"
                onClick={confirmClear}
                className="flex-1 rounded-2xl bg-destructive py-2.5 text-sm font-semibold text-white active:scale-[0.98]"
              >
                清空
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
