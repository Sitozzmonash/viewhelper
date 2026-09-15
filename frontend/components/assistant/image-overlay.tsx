'use client'

/**
 * Enlarged screenshot overlay.
 *
 * Same interaction pattern as AnswerOverlay: a dark translucent backdrop with
 * a slight blur, and tapping anywhere (backdrop or image) collapses it back.
 * Uses z-50 so it sits above the AnswerOverlay (z-40).
 */
export function ImageOverlay({
  src,
  alt = '截图预览',
  onClose,
}: {
  src: string
  alt?: string
  onClose: () => void
}) {
  return (
    <div
      className="absolute inset-0 z-50 flex flex-col items-center justify-center bg-black/55 p-2 backdrop-blur-[2px]"
      onClick={onClose}
      role="presentation"
    >
      {/* eslint-disable-next-line @next/next/no-img-element */}
      <img
        src={src}
        alt={alt}
        className="max-h-[92%] max-w-full rounded-xl object-contain shadow-lg"
        onClick={onClose}
      />
      <p className="mt-2 text-center text-[11px] text-muted-foreground">点击任意处收起</p>
    </div>
  )
}
