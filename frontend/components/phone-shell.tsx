import { cn } from '@/lib/utils'

/**
 * Full-bleed on phones and full-screen again from ``md`` up, so tablet and
 * desktop use the whole viewport. Only the small-landscape band between ``sm``
 * and ``md`` keeps the centered device frame that makes the mobile-first
 * layout readable in a windowed browser.
 */
export function PhoneShell({
  children,
  className,
}: {
  children: React.ReactNode
  className?: string
}) {
  return (
    <div className="flex min-h-dvh w-full justify-center bg-secondary sm:items-center sm:p-6 md:p-0">
      <div
        className={cn(
          'relative flex h-dvh w-full max-w-[430px] flex-col overflow-hidden bg-background',
          'sm:h-[880px] sm:max-h-[94dvh] sm:rounded-[2.5rem] sm:border sm:border-border',
          'sm:shadow-[0_30px_80px_-40px_rgba(15,23,42,0.45)]',
          'md:h-dvh md:max-h-none md:max-w-none md:rounded-none md:border-0 md:shadow-none',
          className,
        )}
      >
        {children}
      </div>
    </div>
  )
}
