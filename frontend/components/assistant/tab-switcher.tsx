'use client'

import { Image as ImageIcon, MessageSquare } from 'lucide-react'
import { cn } from '@/lib/utils'

export type AssistantTab = 'live' | 'screenshot'

const TABS = [
  { id: 'live', label: '实时对话', icon: MessageSquare },
  { id: 'screenshot', label: '截图回答', icon: ImageIcon },
] as const satisfies readonly { id: AssistantTab; label: string; icon: typeof MessageSquare }[]

export function TabSwitcher({
  value,
  onChange,
}: {
  value: AssistantTab
  onChange: (tab: AssistantTab) => void
}) {
  return (
    <div role="tablist" aria-label="助手模式" className="mx-5 mb-3 flex gap-1 rounded-2xl bg-secondary p-1">
      {TABS.map((tab) => {
        const active = value === tab.id
        const Icon = tab.icon
        return (
          <button
            key={tab.id}
            type="button"
            role="tab"
            aria-selected={active}
            onClick={() => onChange(tab.id)}
            className={cn(
              'flex flex-1 items-center justify-center gap-2 rounded-xl py-2.5 text-sm font-medium',
              'transition-all duration-300 ease-out active:scale-[0.97]',
              active
                ? 'bg-primary text-primary-foreground shadow-sm shadow-primary/30'
                : 'text-muted-foreground hover:text-foreground',
            )}
          >
            <Icon className="size-4" />
            {tab.label}
          </button>
        )
      })}
    </div>
  )
}
