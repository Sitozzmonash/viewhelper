import type { Metadata } from 'next'
import { AssistantScreen } from '@/components/assistant/assistant-screen'

export const metadata: Metadata = {
  title: 'AI 助手 · 实时对话与截图回答',
  description: '实时监听对话并让 AI 逐句回答，或截取 PC 屏幕交给视觉模型解读。',
}

export default function AssistantPage() {
  return <AssistantScreen />
}
