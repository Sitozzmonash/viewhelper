import { cn } from '@/lib/utils'

/** Animated audio-level bars used for the listening indicator. */
export function Waveform({
  bars = 5,
  className,
  barClassName,
}: {
  bars?: number
  className?: string
  barClassName?: string
}) {
  return (
    <div className={cn('flex h-4 items-center gap-[3px]', className)} aria-hidden="true">
      {Array.from({ length: bars }).map((_, index) => (
        <span
          key={index}
          className={cn('h-full w-[3px] origin-center rounded-full bg-current', barClassName)}
          style={{ animation: `wave 1.1s ease-in-out ${index * 0.13}s infinite` }}
        />
      ))}
    </div>
  )
}
