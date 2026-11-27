import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";
import { api } from "../../api";
import { toFa } from "../../lib/format";
import { haptic } from "../../lib/telegram";
import { Icon } from "../../ui/Icon";
import { Button, ScreenHeader } from "../../ui/primitives";
import { EcgLoader } from "../../ui/Ecg";
import { EmptyState, ErrorState, ListSkeleton } from "../../ui/states";
import { ExploreCard } from "./ExploreCard";
import { PREFETCH_MARGIN, useExploreFeed } from "./useExploreFeed";

/**
 * یک‌سؤال — the continuous academic feed.
 *
 * Interaction model: one question per viewport, moved by the phone's own
 * vertical scroll with CSS snap points. There is no carousel library, no
 * horizontal swiper, and no "next" button driving the feed — scrolling *is*
 * the navigation, which is what makes it feel like a feed rather than a quiz.
 *
 * Everything else is chosen for the WebView it runs in:
 *  * `overscroll-contain` keeps a hard flick at the end from bouncing the
 *    Telegram sheet;
 *  * the snap container is the scroll parent, so `position: sticky` headers and
 *    `IntersectionObserver` both work without manual scroll math;
 *  * the next page is requested when the reader is three cards from the end, so
 *    it is in memory before the last card is on screen;
 *  * engagement is reported per card (one event), never per scroll pixel.
 */
export function ExploreTab({ onReward }: { onReward: () => void }) {
  const feed = useExploreFeed();
  const [activeIndex, setActiveIndex] = useState(0);
  const [answeredCount, setAnsweredCount] = useState(0);
  const scrollerRef = useRef<HTMLDivElement | null>(null);
  const sentinelRef = useRef<HTMLDivElement | null>(null);
  // Height of one snap viewport. Measured rather than guessed: the header, the
  // Telegram safe area and the fixed tab bar all change it, and a card that
  // overflows the snap point makes the feed feel broken.
  const [cardHeight, setCardHeight] = useState<number | null>(null);
  // Questions whose engagement event has already been sent this session.
  const reported = useRef<Set<number>>(new Set());

  const items = feed.items;
  const { loadMore } = feed;

  useLayoutEffect(() => {
    const surface = scrollerRef.current;
    if (!surface || typeof window === "undefined") return;
    const measure = () => {
      const top = surface.getBoundingClientRect().top;
      // 96px keeps the last card clear of the fixed bottom navigation.
      setCardHeight(Math.max(320, Math.round(window.innerHeight - top - 96)));
    };
    measure();
    window.addEventListener("resize", measure);
    window.addEventListener("orientationchange", measure);
    return () => {
      window.removeEventListener("resize", measure);
      window.removeEventListener("orientationchange", measure);
    };
  }, []);

  // Prefetch: watch a sentinel a few cards before the end. When it enters the
  // viewport (or nears it), ask for the next page. `loadMore` refuses to run
  // while a request is in flight, so a fast flick cannot stampede the server.
  useEffect(() => {
    const target = sentinelRef.current;
    const root = scrollerRef.current;
    if (!target || !root) return;
    if (typeof IntersectionObserver === "undefined") return;
    const observer = new IntersectionObserver(
      (entries) => {
        if (entries.some((entry) => entry.isIntersecting)) loadMore();
      },
      { root, rootMargin: "0px 0px 60% 0px", threshold: 0.01 },
    );
    observer.observe(target);
    return () => observer.disconnect();
  }, [loadMore, items.length]);

  // Which card is the snapped one? The observer reports the entry closest to
  // the top of the viewport, so dwell and impressions follow the reader.
  useEffect(() => {
    const root = scrollerRef.current;
    if (!root || typeof IntersectionObserver === "undefined") return;
    const cards = Array.from(root.querySelectorAll<HTMLElement>("[data-feed-index]"));
    if (!cards.length) return;
    const observer = new IntersectionObserver(
      (entries) => {
        const visible = entries
          .filter((entry) => entry.isIntersecting)
          .sort((a, b) => b.intersectionRatio - a.intersectionRatio)[0];
        if (!visible) return;
        const index = Number(visible.target.getAttribute("data-feed-index"));
        if (!Number.isNaN(index)) setActiveIndex(index);
      },
      { root, threshold: [0.55, 0.75] },
    );
    cards.forEach((card) => observer.observe(card));
    return () => observer.disconnect();
  }, [items.length]);

  // One engagement event per card, fire-and-forget: analytics must never block
  // the feed, and a duplicate must never skew the recommender's signals.
  const onEngagement = useCallback(
    (questionId: number, dwellMs: number, sourceOpened: boolean) => {
      if (reported.current.has(questionId)) return;
      reported.current.add(questionId);
      void api
        .sendExploreEngagement(questionId, Math.max(0, Math.round(dwellMs)), sourceOpened)
        .catch(() => undefined);
    },
    [],
  );

  const onAnswered = useCallback(() => {
    setAnsweredCount((count) => count + 1);
  }, []);

  const restart = () => {
    haptic("tap");
    reported.current = new Set();
    setAnsweredCount(0);
    setActiveIndex(0);
    // Older WebViews may not implement scrollTo on elements; the feed is
    // still usable, the reader just keeps their position.
    scrollerRef.current?.scrollTo?.({ top: 0 });
    feed.restart();
  };

  if (feed.loading && items.length === 0) {
    return (
      <div className="space-y-4">
        <ScreenHeader title="یک‌سؤال" subtitle="فید پیوستهٔ سؤال‌های کوتاه." />
        <ListSkeleton rows={3} />
      </div>
    );
  }

  if (feed.error && items.length === 0) {
    return (
      <div className="space-y-4">
        <ScreenHeader title="یک‌سؤال" />
        <ErrorState message={feed.error} onRetry={feed.loadMore} />
      </div>
    );
  }

  if (!feed.loading && items.length === 0) {
    return (
      <div className="space-y-4">
        <ScreenHeader
          title="یک‌سؤال"
          subtitle="سؤال‌های کوتاه با منبع علمی، برای مرور در زمان‌های مرده."
        />
        <EmptyState
          emblem="network"
          title="فعلاً سؤالی برای نمایش نیست"
          description="همهٔ سؤال‌های این دور را دیده‌اید. با سؤال‌های تازه ادامه دهید."
          action={
            <Button variant="ghost" icon="refresh" onClick={restart}>
              سؤال‌های تازه
            </Button>
          }
        />
      </div>
    );
  }

  return (
    <div className="flex flex-col">
      <ScreenHeader
        title="یک‌سؤال"
        subtitle="با کشیدن انگشت، سؤال بعدی را ببینید."
        actions={
          <span className="flex items-center gap-1 rounded-pill border border-line bg-surface px-2.5 py-1 text-[11px] text-muted">
            <Icon name="spark" size={13} className="text-brand" />
            <span className="nums">{toFa(answeredCount)} پاسخ</span>
          </span>
        }
      />

      {/*
        The scroll surface. `snap-y snap-mandatory` turns plain touch scrolling
        into a page-at-a-time feed with zero JavaScript, and the height is the
        viewport minus the header and the fixed tab bar, so a card never spills
        under the navigation.
      */}
      <div
        ref={scrollerRef}
        data-testid="explore-feed"
        role="feed"
        tabIndex={0}
        aria-label="فید سؤال‌های یک‌سؤال"
        className="no-scrollbar snap-y snap-mandatory overflow-y-auto overscroll-contain"
        style={{ height: cardHeight ? `${cardHeight}px` : "calc(100dvh - 190px)" }}
      >
        {items.map((item, index) => (
          <div
            key={item.question_id}
            data-feed-index={index}
            className="flex snap-start snap-always items-stretch"
            style={{ minHeight: "100%" }}
          >
            <ExploreCard
              item={item}
              position={index + 1}
              active={index === activeIndex}
              onEngagement={onEngagement}
              onReward={onReward}
              onAnswered={onAnswered}
            />
          </div>
        ))}

        {/* The prefetch sentinel: three cards before the end of what we have. */}
        {items.length > PREFETCH_MARGIN && (
          <div
            ref={sentinelRef}
            aria-hidden
            data-testid="explore-sentinel"
            className="pointer-events-none absolute h-px w-px"
            style={{ marginTop: `-${(PREFETCH_MARGIN + 1) * 100}%` }}
          />
        )}

        {/* ------------------------------------------------------------- tail */}
        <div className="flex snap-start flex-col items-center justify-center gap-2 px-4 py-6 text-center">
          {feed.loadingMore && (
            <div className="flex flex-col items-center gap-1.5">
              <EcgLoader label="در حال آوردن سؤال‌های بعدی" className="text-trace" />
              <p className="text-[11px] text-faint">سؤال‌های بعدی در راه است…</p>
            </div>
          )}

          {feed.error && (
            <div className="space-y-2" role="alert">
              <p className="text-[11px] leading-6 text-faint">{feed.error}</p>
              <Button variant="secondary" icon="refresh" onClick={feed.loadMore}>
                تلاش دوباره
              </Button>
            </div>
          )}

          {feed.exhausted && (
            <div className="space-y-2">
              <p className="text-[11px] leading-6 text-faint">
                سؤال تازه‌ای برای این دور نمانده است.
              </p>
              <Button variant="secondary" icon="refresh" onClick={restart}>
                سؤال‌های تازه
              </Button>
            </div>
          )}

          {!feed.exhausted && !feed.error && !feed.loadingMore && (
            <Button variant="ghost" icon="forward" onClick={feed.loadMore}>
              سؤال‌های بیشتر
            </Button>
          )}
        </div>
      </div>
    </div>
  );
}
