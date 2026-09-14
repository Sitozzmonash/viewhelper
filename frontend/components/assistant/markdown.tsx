'use client'

import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'

/** Renders the AI answer body: headings, lists, bold, inline + fenced code. */
export function Markdown({ children }: { children: string }) {
  return (
    <div className="text-[15px] leading-relaxed text-foreground/90">
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        components={{
          h1: ({ children }) => (
            <h3 className="mt-4 mb-2 text-base font-semibold text-foreground first:mt-0">{children}</h3>
          ),
          h2: ({ children }) => (
            <h3 className="mt-4 mb-2 text-base font-semibold text-foreground first:mt-0">{children}</h3>
          ),
          h3: ({ children }) => (
            <h4 className="mt-4 mb-2 text-[15px] font-semibold text-foreground first:mt-0">{children}</h4>
          ),
          h4: ({ children }) => (
            <h5 className="mt-3 mb-1.5 text-sm font-semibold text-foreground first:mt-0">{children}</h5>
          ),
          p: ({ children }) => <p className="my-2 first:mt-0 last:mb-0">{children}</p>,
          ul: ({ children }) => <ul className="my-2 list-disc space-y-1 pl-5">{children}</ul>,
          ol: ({ children }) => <ol className="my-2 list-decimal space-y-1 pl-5">{children}</ol>,
          li: ({ children }) => <li className="pl-0.5">{children}</li>,
          strong: ({ children }) => (
            <strong className="font-semibold text-foreground">{children}</strong>
          ),
          a: ({ children, href }) => (
            <a href={href} className="text-primary underline underline-offset-2" target="_blank" rel="noreferrer">
              {children}
            </a>
          ),
          blockquote: ({ children }) => (
            <blockquote className="my-2 border-l-2 border-ai/40 pl-3 text-muted-foreground">
              {children}
            </blockquote>
          ),
          hr: () => <hr className="my-3 border-ai-border" />,
          pre: ({ children }) => (
            <pre className="my-3 overflow-x-auto rounded-xl bg-foreground/95 p-3 text-xs leading-relaxed text-background">
              {children}
            </pre>
          ),
          code: ({ className, children }) => {
            const isBlock = /language-/.test(className ?? '')
            if (isBlock) {
              return <code className="font-mono">{children}</code>
            }
            return (
              <code className="rounded-md bg-ai/12 px-1.5 py-0.5 font-mono text-[0.85em] text-ai-foreground">
                {children}
              </code>
            )
          },
        }}
      >
        {children}
      </ReactMarkdown>
    </div>
  )
}
