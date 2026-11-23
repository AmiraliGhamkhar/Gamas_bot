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

export function describeError(err: unknown): string {
  if (err instanceof ApiError) {
    if (err.status === 401) return "نشست تلگرام شما معتبر نیست؛ مینی‌اپ را دوباره باز کنید.";
    if (err.status === 403) return "دسترسی به این بخش برای حساب شما باز نیست.";
    if (err.status === 404) return "این مورد دیگر در دسترس نیست.";
    if (err.status >= 500) return "سرور گاماس پاسخ نداد؛ کمی بعد دوباره تلاش کنید.";
    return err.message;
  }
  if (err instanceof Error && err.message) return err.message;
  return "خطای نامشخص؛ دوباره تلاش کنید.";
}
