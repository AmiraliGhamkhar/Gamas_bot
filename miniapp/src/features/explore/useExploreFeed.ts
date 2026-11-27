import { useCallback, useEffect, useRef, useState } from "react";
import { api, ExploreItem } from "../../api";

/**
 * The state machine behind the continuous Explore feed.
 *
 * It owns everything the view should not have to think about:
 *
 * * **cursor pagination** — the first page loads on mount, the next one is
 *   fetched when the user is within `PREFETCH_MARGIN` cards of the end, so the
 *   questions are already in memory before the last card is reached;
 * * **no duplicates** — question ids are de-duplicated as pages arrive. The
 *   server's cursor already forbids a repeat inside a session; this guards the
 *   client against a racing prefetch merging the same page twice;
 * * **a failing page never breaks the feed** — already-loaded cards stay, the
 *   error surfaces as a small retry state at the tail, and scrolling continues;
 * * **session end** — once the server says `has_more: false` the hook exposes
 *   `exhausted`, so the tail can offer a fresh session instead of an endless
 *   spinner;
 * * **image-free, dependency-free** — this is a Telegram WebView on a phone.
 */

/** Cards left below the viewport that trigger the next page. */
export const PREFETCH_MARGIN = 3;

/** Cards per page: enough to fill a phone screen, small enough to be instant. */
export const PAGE_SIZE = 12;

export interface ExploreFeedState {
  items: ExploreItem[];
  loading: boolean;
  loadingMore: boolean;
  /** A page request failed. The loaded cards are still valid. */
  error: string | null;
  /** The server has no more unseen questions for this session. */
  exhausted: boolean;
  /** Fetch the next page now (idempotent while a request is in flight). */
  loadMore: () => void;
  /** Discard the session and start a fresh feed. */
  restart: () => void;
}

function mergeUnique(existing: ExploreItem[], incoming: ExploreItem[]): ExploreItem[] {
  if (!incoming.length) return existing;
  const seen = new Set(existing.map((item) => item.question_id));
  const next = existing.slice();
  for (const item of incoming) {
    if (seen.has(item.question_id)) continue;
    seen.add(item.question_id);
    next.push(item);
  }
  return next;
}

export function useExploreFeed(): ExploreFeedState {
  const [items, setItems] = useState<ExploreItem[]>([]);
  const [hasMore, setHasMore] = useState(true);
  const [loading, setLoading] = useState(true);
  const [loadingMore, setLoadingMore] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [generation, setGeneration] = useState(0);

  // Refs mirror the state the request path needs so `loadMore` stays stable
  // and can never fire twice for the same cursor.
  const cursorRef = useRef("");
  const hasMoreRef = useRef(true);
  const inFlight = useRef(false);

  const fetchPage = useCallback(
    async (token: string, gen: number) => {
      if (inFlight.current) return;
      inFlight.current = true;
      const isFirst = !token;
      if (isFirst) setLoading(true);
      else setLoadingMore(true);
      setError(null);
      try {
        const page = await api.getExploreFeed(token, PAGE_SIZE);
        if (gen !== generation) return;
        setItems((prev) => mergeUnique(isFirst ? [] : prev, page.items));
        cursorRef.current = page.next_cursor;
        hasMoreRef.current = Boolean(page.has_more && page.next_cursor);
        setHasMore(hasMoreRef.current);
      } catch {
        if (gen !== generation) return;
        // Keep what is on screen; the tail shows a compact retry. The message
        // is deliberately generic - the underlying failure is a network
        // detail the reader can do nothing with.
        setError("بارگیری سؤال‌های بعدی انجام نشد.");
      } finally {
        if (gen === generation) {
          setLoading(false);
          setLoadingMore(false);
        }
        inFlight.current = false;
      }
    },
    [generation],
  );

  // First page (and every restart) loads immediately.
  useEffect(() => {
    cursorRef.current = "";
    hasMoreRef.current = true;
    setItems([]);
    setHasMore(true);
    void fetchPage("", generation);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [generation]);

  const loadMore = useCallback(() => {
    if (inFlight.current || !hasMoreRef.current) return;
    void fetchPage(cursorRef.current, generation);
  }, [fetchPage, generation]);

  const restart = useCallback(() => {
    inFlight.current = false;
    setGeneration((gen) => gen + 1);
  }, []);

  return {
    items,
    loading,
    loadingMore,
    error,
    exhausted: !hasMore && !loading && items.length > 0,
    // `refresh` for the retry button: same cursor, same generation.
    loadMore: error ? () => void fetchPage(cursorRef.current, generation) : loadMore,
    restart,
  };
}
