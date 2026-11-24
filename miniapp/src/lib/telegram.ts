/** The slice of the Telegram WebApp SDK this app actually uses. */
export interface TelegramUser {
  id?: number;
  first_name?: string;
  last_name?: string;
  username?: string;
  photo_url?: string;
  language_code?: string;
}

interface TelegramThemeParams {
  bg_color?: string;
  secondary_bg_color?: string;
  text_color?: string;
  hint_color?: string;
  link_color?: string;
  button_color?: string;
  button_text_color?: string;
}

interface TelegramWebApp {
  initData?: string;
  initDataUnsafe?: { user?: TelegramUser; start_param?: string };
  colorScheme?: "light" | "dark";
  themeParams?: TelegramThemeParams;
  version?: string;
  ready?: () => void;
  expand?: () => void;
  setHeaderColor?: (color: string) => void;
  setBackgroundColor?: (color: string) => void;
  disableVerticalSwipes?: () => void;
  openTelegramLink?: (url: string) => void;
  isVersionAtLeast?: (version: string) => boolean;
  HapticFeedback?: {
    impactOccurred?: (style: "light" | "medium" | "heavy" | "rigid" | "soft") => void;
    notificationOccurred?: (type: "error" | "success" | "warning") => void;
    selectionChanged?: () => void;
  };
}

type TelegramRoot = { WebApp?: TelegramWebApp };

function root(): TelegramWebApp | undefined {
  if (typeof window === "undefined") return undefined;
  return (window as unknown as { Telegram?: TelegramRoot }).Telegram?.WebApp;
}

/** Raw initData, forwarded to the server on every authenticated call. */
export function getInitData(): string {
  return root()?.initData ?? "";
}

export function getTelegramUser(): TelegramUser {
  return root()?.initDataUnsafe?.user ?? {};
}

/** The app only ever runs inside Telegram; outside, the shell says so. */
export function isInsideTelegram(): boolean {
  return Boolean(root()?.initData);
}

/** The host's colour scheme; the app ships its own navy theme and only listens. */
export function getColorScheme(): "light" | "dark" {
  return root()?.colorScheme ?? "dark";
}

/** Raw theme params from the host — read for awareness, never mixed into ours. */
export function getThemeParams(): TelegramThemeParams {
  return root()?.themeParams ?? {};
}

/** Gamas navy ground: the Palette navy family darkened for the app chrome. */
const APP_GROUND = "#03181F";

/** One-time SDK boot: fill the screen, adopt our navy ground, tame swipes. */
export function initTelegram(): void {
  const app = root();
  if (!app) return;
  const safe = (fn: (() => void) | undefined) => {
    try {
      fn?.();
    } catch {
      /* an older client may not implement the call */
    }
  };
  safe(app.ready);
  safe(app.expand);
  safe(() => app.setHeaderColor?.(APP_GROUND));
  safe(() => app.setBackgroundColor?.(APP_GROUND));
  if (typeof document !== "undefined") {
    document
      .querySelector('meta[name="theme-color"]')
      ?.setAttribute("content", APP_GROUND);
  }
  // Vertical swipes belong to the page (the یک‌سؤال feed is a snap list).
  if (app.isVersionAtLeast?.("7.7")) safe(app.disableVerticalSwipes);
}

export function haptic(kind: "tap" | "success" | "error" | "select"): void {
  const haptics = root()?.HapticFeedback;
  if (!haptics) return;
  try {
    if (kind === "tap") haptics.impactOccurred?.("light");
    else if (kind === "select") haptics.selectionChanged?.();
    else haptics.notificationOccurred?.(kind);
  } catch {
    /* haptics are a nicety, never a dependency */
  }
}

/** Open a t.me link inside Telegram when possible. */
export function openTelegramLink(url: string): void {
  const app = root();
  try {
    if (app?.openTelegramLink) {
      app.openTelegramLink(url);
      return;
    }
  } catch {
    /* fall through to a plain navigation */
  }
  if (typeof window !== "undefined") window.open(url, "_blank", "noopener");
}

export function shareLink(url: string, text?: string): void {
  const query = new URLSearchParams({ url, ...(text ? { text } : {}) });
  openTelegramLink(`https://t.me/share/url?${query.toString()}`);
}

/** Clipboard with a fallback for WebViews without the async API. */
export async function copyText(text: string): Promise<boolean> {
  try {
    if (navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(text);
      return true;
    }
  } catch {
    /* fall back below */
  }
  try {
    const area = document.createElement("textarea");
    area.value = text;
    area.setAttribute("readonly", "");
    area.style.position = "fixed";
    area.style.opacity = "0";
    document.body.appendChild(area);
    area.select();
    const done = document.execCommand("copy");
    document.body.removeChild(area);
    return done;
  } catch {
    return false;
  }
}
