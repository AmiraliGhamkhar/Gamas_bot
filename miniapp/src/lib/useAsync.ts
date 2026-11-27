import { useCallback, useEffect, useRef, useState } from "react";
import { ApiError } from "../api";

export interface AsyncState<T> {
  data: T | null;
  error: string | null;
  /** HTTP status of the failure, when it came from the API. */
  status: number | null;
  loading: boolean;
  reload: () => void;
}

/**
 * One loader, four honest states (loading, ready, empty-by-data, error).
 *
 * A stale response can never overwrite a newer one, and a reload keeps the last
 * good data on screen while it refreshes — the tab does not blink to a skeleton.
 */
export function useAsync<T>(
  loader: () => Promise<T>,
  deps: readonly unknown[] = [],
): AsyncState<T> {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [status, setStatus] = useState<number | null>(null);
  const [loading, setLoading] = useState(true);
  const [nonce, setNonce] = useState(0);
  const runRef = useRef(0);

  const reload = useCallback(() => setNonce((n) => n + 1), []);

  useEffect(() => {
    const run = ++runRef.current;
    let alive = true;
    setLoading(true);
    setError(null);
    setStatus(null);
    loader()
      .then((result) => {
        if (!alive || run !== runRef.current) return;
        setData(result);
      })
      .catch((err: unknown) => {
        if (!alive || run !== runRef.current) return;
        setError(describeError(err));
        setStatus(err instanceof ApiError ? err.status : null);
      })
      .finally(() => {
        if (!alive || run !== runRef.current) return;
        setLoading(false);
      });
    return () => {
      alive = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [...deps, nonce]);

  return { data, error, status, loading, reload };
}

/** Server error strings the API can return, spoken in Persian to the user. */
const API_MESSAGES: Record<string, string> = {
  "insufficient points": "امتیاز شما برای این جایزه کافی نیست.",
  "usage limit reached": "سقف استفاده از این جایزه رسیده است.",
  "invalid or disabled reward_code": "این جایزه در دسترس نیست.",
  redemption_failed: "تبدیل امتیاز انجام نشد؛ چیزی از حساب شما کم نشد.",
  redemption_not_completed: "تبدیل امتیاز انجام نشد.",
  "invalid payload": "درخواست معتبر نبود؛ دوباره تلاش کنید.",
  "not found": "این مورد دیگر در دسترس نیست.",
  forbidden: "دسترسی به این بخش برای حساب شما باز نیست.",
};

export function describeError(err: unknown): string {
  if (err instanceof ApiError) {
    if (err.status === 401) return "نشست تلگرام شما معتبر نیست؛ مینی‌اپ را دوباره باز کنید.";
    if (err.status === 403) return "دسترسی به این بخش برای حساب شما باز نیست.";
    if (err.status === 404) return "این مورد دیگر در دسترس نیست.";
    if (err.status >= 500) return "سرور گاماس پاسخ نداد؛ کمی بعد دوباره تلاش کنید.";
    return API_MESSAGES[err.message] ?? err.message;
  }
  // A fetch that never reached the server throws TypeError with a browser-
  // specific English message; the user reads Persian, never "Failed to fetch".
  const raw = err instanceof Error ? err.message : "";
  if (
    err instanceof TypeError ||
    /failed to fetch|networkerror|network error|fetch failed/i.test(raw)
  ) {
    return "اتصال به سرور برقرار نشد؛ اینترنت خود را بررسی کنید و دوباره تلاش کنید.";
  }
  if (raw) return raw;
  return "خطای نامشخص؛ دوباره تلاش کنید.";
}
