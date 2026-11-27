/**
 * The app's whole icon vocabulary: twenty-four line icons, one stroke weight.
 *
 * Drawings are inline so they inherit `currentColor` (and therefore the theme),
 * stay crisp on any density and add no icon-font request to the Mini App.
 */

export type IconName =
  | "account"
  | "library"
  | "review"
  | "explore"
  | "download"
  | "back"
  | "forward"
  | "check"
  | "close"
  | "alert"
  | "refresh"
  | "copy"
  | "share"
  | "spark"
  | "trend"
  | "doc"
  | "sheet"
  | "audio"
  | "video"
  | "text"
  | "cards"
  | "lock"
  | "clock"
  | "wallet"
  | "search"
  | "trash"
  | "send"
  | "info"
  | "chevron-up"
  | "chevron-down";

const PATHS: Record<IconName, JSX.Element> = {
  account: (
    <>
      <circle cx="12" cy="8.5" r="3.5" />
      <path d="M4.5 19.5c1.4-3.2 4.2-4.8 7.5-4.8s6.1 1.6 7.5 4.8" />
    </>
  ),
  library: (
    <>
      <path d="M5 4.5h9.5A2.5 2.5 0 0 1 17 7v13H7.5A2.5 2.5 0 0 1 5 17.5z" />
      <path d="M17 7h1.5A1.5 1.5 0 0 1 20 8.5v9a1.5 1.5 0 0 1-1.5 1.5H17" />
      <path d="M8.5 8.5h5" />
    </>
  ),
  review: (
    <>
      <rect x="4" y="4.5" width="16" height="15" rx="3" />
      <path d="M7.5 12l2.6 2.6 5.4-5.6" />
    </>
  ),
  explore: (
    <>
      <circle cx="12" cy="12" r="8.2" />
      <path d="M14.9 9.1l-1.7 4.1-4.1 1.7 1.7-4.1z" />
    </>
  ),
  download: (
    <>
      <path d="M12 4.5v9" />
      <path d="M8.4 10.2L12 13.8l3.6-3.6" />
      <path d="M5 17.5v1.2A1.8 1.8 0 0 0 6.8 20.5h10.4a1.8 1.8 0 0 0 1.8-1.8v-1.2" />
    </>
  ),
  back: <path d="M9.5 5.5L16 12l-6.5 6.5" />,
  forward: <path d="M14.5 5.5L8 12l6.5 6.5" />,
  check: <path d="M5 12.8l4.4 4.4L19 6.6" />,
  close: (
    <>
      <path d="M6.5 6.5l11 11" />
      <path d="M17.5 6.5l-11 11" />
    </>
  ),
  alert: (
    <>
      <path d="M12 4.6l8 14H4z" />
      <path d="M12 10v3.6" />
      <circle cx="12" cy="16.4" r="0.5" fill="currentColor" stroke="none" />
    </>
  ),
  refresh: (
    <>
      <path d="M19 12a7 7 0 1 1-2.3-5.2" />
      <path d="M19.2 4.8v4.4h-4.4" />
    </>
  ),
  copy: (
    <>
      <rect x="8.5" y="8.5" width="10" height="10" rx="2.4" />
      <path d="M15.5 8.5V7.4A2.4 2.4 0 0 0 13.1 5H7.4A2.4 2.4 0 0 0 5 7.4v5.7a2.4 2.4 0 0 0 2.4 2.4H8.5" />
    </>
  ),
  share: (
    <>
      <path d="M12 15.5V4.8" />
      <path d="M8.4 8.2L12 4.6l3.6 3.6" />
      <path d="M5.5 13.5v4.2A1.8 1.8 0 0 0 7.3 19.5h9.4a1.8 1.8 0 0 0 1.8-1.8v-4.2" />
    </>
  ),
  spark: (
    <>
      <path d="M12 4.5l1.7 4.3 4.3 1.7-4.3 1.7L12 16.5l-1.7-4.3L6 10.5l4.3-1.7z" />
    </>
  ),
  trend: (
    <>
      <path d="M4.5 16.5l4.6-4.6 3 3 6.4-6.4" />
      <path d="M14.5 8.5h4v4" />
    </>
  ),
  doc: (
    <>
      <path d="M7 4.5h6.5L18 9v10.5H7z" />
      <path d="M13 4.5V9h5" />
      <path d="M9.5 13h5" />
      <path d="M9.5 16h3.2" />
    </>
  ),
  sheet: (
    <>
      <rect x="4.5" y="5.5" width="15" height="13" rx="2.2" />
      <path d="M4.5 10h15" />
      <path d="M10 10v8.5" />
    </>
  ),
  audio: (
    <>
      <path d="M5 10.5v3" />
      <path d="M8.2 8.5v7" />
      <path d="M11.4 6v12" />
      <path d="M14.6 8.5v7" />
      <path d="M17.8 10.5v3" />
    </>
  ),
  video: (
    <>
      <rect x="4.5" y="6.5" width="11" height="11" rx="2.4" />
      <path d="M15.5 11l4-2.4v6.8l-4-2.4z" />
    </>
  ),
  text: (
    <>
      <path d="M5.5 7.5h13" />
      <path d="M5.5 12h13" />
      <path d="M5.5 16.5h8" />
    </>
  ),
  cards: (
    <>
      <rect x="7.5" y="4.5" width="11" height="11" rx="2.6" />
      <path d="M15.5 18.5H8A2.5 2.5 0 0 1 5.5 16V8.5" />
    </>
  ),
  lock: (
    <>
      <rect x="5.5" y="10.5" width="13" height="9" rx="2.4" />
      <path d="M8.8 10.5V8.2a3.2 3.2 0 0 1 6.4 0v2.3" />
    </>
  ),
  clock: (
    <>
      <circle cx="12" cy="12" r="8.2" />
      <path d="M12 7.8V12l2.8 1.7" />
    </>
  ),
  wallet: (
    <>
      <rect x="4.5" y="6.5" width="15" height="11.5" rx="2.6" />
      <path d="M4.5 10.5h15" />
      <path d="M15.4 14.2h4.1v3.1h-4.1a1.55 1.55 0 0 1 0-3.1z" />
    </>
  ),
  search: (
    <>
      <circle cx="11" cy="11" r="6.2" />
      <path d="M15.6 15.6L19.5 19.5" />
    </>
  ),
  trash: (
    <>
      <path d="M5.5 7.5h13" />
      <path d="M9.5 7.5V5.8h5v1.7" />
      <path d="M7 7.5l.8 11a1.6 1.6 0 0 0 1.6 1.5h5.2a1.6 1.6 0 0 0 1.6-1.5l.8-11" />
    </>
  ),
  info: (
    <>
      <circle cx="12" cy="12" r="8" />
      <path d="M12 11v5.4" />
      <circle cx="12" cy="8" r="0.6" fill="currentColor" stroke="none" />
    </>
  ),
  "chevron-up": <path d="M6.5 14.5L12 9l5.5 5.5" />,
  "chevron-down": <path d="M6.5 9.5L12 15l5.5-5.5" />,
  send: (
    <>
      <path d="M19.5 4.5L4.8 10.6l5.3 2.3 2.3 5.3z" />
      <path d="M19.5 4.5l-9.4 8.4" />
    </>
  ),
};

export function Icon({
  name,
  size = 20,
  className = "",
  strokeWidth = 1.7,
}: {
  name: IconName;
  size?: number;
  className?: string;
  strokeWidth?: number;
}) {
  return (
    <svg
      viewBox="0 0 24 24"
      width={size}
      height={size}
      fill="none"
      stroke="currentColor"
      strokeWidth={strokeWidth}
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      focusable="false"
      className={className}
    >
      {PATHS[name]}
    </svg>
  );
}
