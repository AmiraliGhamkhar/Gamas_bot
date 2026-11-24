/**
 * The Gamas pulse: the ECG trace from the booklet system (`decorative/ecg-line`,
 * the brand monogram), redrawn as SVG so it stays crisp at any width, inherits
 * `currentColor` and can be animated. It is the app's one decorative motif:
 * a quiet rule along the top of every paper card and the loading indicator.
 */

const TRACE =
  "M0 6 H70 l5 -5 4 9 4 -9 5 5 H140 l5 -5 4 9 4 -9 5 5 H210 " +
  "l5 -5 4 9 4 -9 5 5 H280 l5 -5 4 9 4 -9 5 5 H350 l5 -5 4 9 4 -9 5 5 " +
  "H420 l5 -5 4 9 4 -9 5 5 H480";

export function EcgRule({ className = "" }: { className?: string }) {
  return (
    <svg
      viewBox="0 0 480 12"
      preserveAspectRatio="none"
      aria-hidden="true"
      focusable="false"
      className={className}
    >
      <path
        d={TRACE}
        fill="none"
        stroke="currentColor"
        strokeWidth={2}
        strokeLinecap="round"
        strokeLinejoin="round"
        vectorEffect="non-scaling-stroke"
      />
    </svg>
  );
}

/** The same trace with one segment sweeping across it — the loading state. */
export function EcgLoader({
  label = "در حال بارگذاری",
  className = "text-brand",
}: {
  label?: string;
  /** Colour hook: `text-brand` on chrome, `text-trace` on paper. */
  className?: string;
}) {
  return (
    <span className="flex flex-col items-center gap-2" role="status">
      <svg
        viewBox="0 0 480 12"
        preserveAspectRatio="none"
        aria-hidden="true"
        focusable="false"
        className={`h-3 w-40 ${className}`}
      >
        <path
          d={TRACE}
          fill="none"
          stroke="currentColor"
          strokeWidth={2}
          strokeLinecap="round"
          strokeLinejoin="round"
          vectorEffect="non-scaling-stroke"
          opacity={0.22}
        />
        <path
          d={TRACE}
          fill="none"
          stroke="currentColor"
          strokeWidth={2.4}
          strokeLinecap="round"
          strokeLinejoin="round"
          vectorEffect="non-scaling-stroke"
          strokeDasharray="90 480"
          className="animate-ecg"
        />
      </svg>
      <span className="sr-only">{label}</span>
    </span>
  );
}
