'use client'

import { useCallback, useState } from 'react'
import { PhoneShell } from '@/components/phone-shell'
import { cn } from '@/lib/utils'
import { AssistantHeader } from './assistant-header'
import { ConversationView } from './conversation-view'
import { ScreenshotView } from './screenshot-view'
import { SettingsPanel } from './settings-panel'
import { TabSwitcher, type AssistantTab } from './tab-switcher'

export function AssistantScreen() {
  const [tab, setTab] = useState<AssistantTab>('live')
  const [settingsOpen, setSettingsOpen] = useState(false)

  const handleChange = useCallback((next: AssistantTab) => setTab(next), [])

  // Both views stay mounted so their subscriptions never miss socket events
  // (e.g. a screenshot arriving while the conversation tab is visible).
  return (
    <PhoneShell>
      <AssistantHeader onOpenSettings={() => setSettingsOpen(true)} />
      <TabSwitcher value={tab} onChange={handleChange} />

      <main className="relative flex min-h-0 flex-1 flex-col">
        <div className={cn('flex min-h-0 flex-1 flex-col', tab !== 'live' && 'hidden')}>
          <ConversationView />
        </div>
        <div className={cn('flex min-h-0 flex-1 flex-col', tab !== 'screenshot' && 'hidden')}>
          <ScreenshotView />
        </div>
      </main>

      <SettingsPanel open={settingsOpen} onClose={() => setSettingsOpen(false)} />
    </PhoneShell>
  )
}
