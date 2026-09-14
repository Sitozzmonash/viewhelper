import { cn } from '@/lib/utils'

/**
 * Full-bleed on phones, a centered device frame on larger screens so the
 * mobile-first layout stays readable on desktop.
 */
export function PhoneShell({
  children,
  className,
}: {
  children: React.ReactNode
  className?: string
}) {
  return (
    <div className="flex min-h-dvh w-full justify-center bg-secondary sm:items-center sm:p-6">
      <div
        className={cn(
          'relative flex h-dvh w-full max-w-[430px] flex-col overflow-hidden bg-background',
          'sm:h-[880px] sm:max-h-[94dvh] sm:rounded-[2.5rem] sm:border sm:border-border',
          'sm:shadow-[0_30px_80px_-40px_rgba(15,23,42,0.45)]',
          className,
        )}
      >
        {children}
      </div>
    </div>
  )
}
