import type { MetadataRoute } from 'next'

/**
 * PWA manifest. White/blue theme matching the app design (PRD §14): white
 * background, blue primary. Installed to the home screen as "AI 实时助手".
 */
export default function manifest(): MetadataRoute.Manifest {
  return {
    name: 'AI 实时助手',
    short_name: 'AI 助手',
    description: 'PC 实时语音 + 截图 AI 助手：实时对话、截图问答、流式 AI 回答。',
    start_url: '/',
    scope: '/',
    display: 'standalone',
    orientation: 'portrait',
    background_color: '#ffffff',
    theme_color: '#ffffff',
    categories: ['productivity', 'utilities'],
    icons: [
      {
        src: '/icon.svg',
        sizes: 'any',
        type: 'image/svg+xml',
        purpose: 'any',
      },
      {
        src: '/icon-light-32x32.png',
        sizes: '32x32',
        type: 'image/png',
      },
      {
        src: '/apple-icon.png',
        sizes: '180x180',
        type: 'image/png',
      },
    ],
  }
}
