import { memo, useEffect, useRef, useState } from "react";
import { api, ExploreAnswerResult, ExploreItem } from "../../api";
import { toFa } from "../../lib/format";
import { haptic } from "../../lib/telegram";
import { EcgLoader } from "../../ui/Ecg";
import { Icon } from "../../ui/Icon";
import {
  Chip,
  PaperCard,
  PaperOption,
  PaperOptionState,
} from "../../ui/primitives";
import { SourcePanel } from "./SourcePanel";

/**
 * One question, one viewport.
 *
 * The card is a *unit of reading*, not a slide: it fills the snap viewport,
 * answers in place, and reveals its explanation and citation without ever
 * pushing the reader to a separate screen. Everything that makes it a card —
 * topic, difficulty, verdict, source — sits inside it, so scrolling alone is
 * enough to move on.
 *
 * Rendering and network rules:
 *  * `memo` + a stable `question_id` key: a new page arriving never re-renders
 *    the cards already on screen;
 *  * the answer POST is fire-and-forget from the scroll's point of view — the
 *    option locks instantly, then the verdict settles;
 *  * the dwell timer starts when the card is first answered-at or scrolled to,
 *    so an impression means "this card was really looked at".
 */

const DIFFICULTY_LABELS: Record<string, string> = {
  easy: "آسان",
  medium: "متوسط",
  hard: "دشوار",
};

export interface ExploreCardProps {
  item: ExploreItem;
  /** Position in the feed, 1-based, for the progress line. */
  position: number;
  /** The card is the snapped one: it may start its dwell clock. */
  active: boolean;
  /** Report this card's one meaningful engagement event. */
  onEngagement: (questionId: number, dwellMs: number, sourceOpened: boolean) => void;
  /** A correct answer was rewarded (parent refreshes the account). */
  onReward: () => void;
  /** The card was answered — the feed may prefetch earlier. */
  onAnswered: (questionId: number) => void;
}

function ExploreCardInner({
  item,
  position,
  active,
  onEngagement,
  onReward,
  onAnswered,
}: ExploreCardProps) {
  const [result, setResult] = useState<ExploreAnswerResult | null>(null);
  const [picked, setPicked] = useState<number | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [earned, setEarned] = useState<number | null>(null);
  const [sourceOpened, setSourceOpened] = useState(false);
  const [impressed, setImpressed] = useState(false);

  const enteredAt = useRef(Date.now());
  const cardRef = useRef<HTMLDivElement | null>(null);

  // Dwell starts when the card becomes the snapped one.
  useEffect(() => {
    if (active) enteredAt.current = Date.now();
  }, [active]);

  // One impression per card per session, sent when it has been the active card
  // long enough to be "seen". Never on scroll pixels, never twice.
  useEffect(() => {
    if (!active || impressed || result) return;
    const timer = window.setTimeout(() => {
      setImpressed(true);
      onEngagement(item.question_id, Date.now() - enteredAt.current, false);
    }, 1200);
    return () => window.clearTimeout(timer);
  }, [active, impressed, result, item.question_id, onEngagement]);

  const locked = Boolean(result) || submitting;

  const answer = async (optionIndex: number) => {
    if (locked) return;
    setSubmitting(true);
    setPicked(optionIndex);
    setError(null);
    setImpressed(true);
    const dwell = Math.max(0, Date.now() - enteredAt.current);
    try {
      const res = await api.submitExploreAnswer(item.question_id, optionIndex, dwell);
      setResult(res);
      haptic(res.is_correct ? "success" : "error");
      if (res.points_awarded > 0) {
        setEarned(res.points_awarded);
        onReward();
      }
      onAnswered(item.question_id);
    } catch {
      // The choice stays visible; the card is unlocked so it can be retried.
      haptic("error");
      setPicked(null);
      setError("ثبت پاسخ انجام نشد؛ دوباره تلاش کنید.");
    } finally {
      setSubmitting(false);
    }
  };

  const tone = result ? (result.is_correct ? "ok" : "bad") : "brand";
  // The answer result carries its own citation; fall back to the feed's copy.
  const source = result?.source ?? item.source ?? null;
  const verified = result ? result.verified !== false : item.verified;

  return (
    <article
      ref={cardRef}
      aria-label={`سؤال ${toFa(position)}: ${item.question_text}`}
      className="flex w-full flex-col justify-center px-4 py-2"
    >
      <PaperCard tone={tone} className="space-y-3">
        {/* ------------------------------------------------ head: topic + rank */}
        <div className="flex items-center justify-between gap-2">
          <div className="flex min-w-0 flex-wrap items-center gap-1.5">
            <Chip tone="brand" onPaper className="max-w-[18ch]">
              <span className="truncate">{item.topic}</span>
            </Chip>
            <Chip onPaper>
              {DIFFICULTY_LABELS[item.difficulty] ?? item.difficulty}
            </Chip>
            {verified ? (
              <Chip tone="ok" onPaper dot>
                منبع‌دار
              </Chip>
            ) : (
              <Chip onPaper>در انتظار راستی‌آزمایی</Chip>
            )}
          </div>
          <span className="nums shrink-0 text-[11px] text-inkMuted">
            {toFa(position)}
          </span>
        </div>

        {/* ------------------------------------------------------- the question */}
        <h2 className="text-[15px] font-medium leading-8 text-ink">{item.question_text}</h2>

        <div className="space-y-2" role="group" aria-label="گزینه‌های پاسخ">
          {item.options.map((option, optionIndex) => {
            let state: PaperOptionState = "idle";
            if (result) {
              if (optionIndex === result.correct_index) state = "correct";
              else if (optionIndex === picked) state = "wrong";
              else state = "muted";
            } else if (optionIndex === picked) {
              state = "chosen";
            }
            return (
              <PaperOption
                key={optionIndex}
                label={option}
                index={optionIndex}
                state={state}
                disabled={locked}
                onPick={() => void answer(optionIndex)}
              />
            );
          })}
        </div>

        {submitting && (
          <div className="flex items-center justify-center gap-2 py-1">
            <EcgLoader label="در حال ثبت پاسخ" className="text-trace" />
            <span className="text-[11px] text-inkMuted">در حال ثبت پاسخ…</span>
          </div>
        )}

        {error && (
          <p className="rounded-field bg-bad/12 px-3 py-2 text-[11px] leading-6 text-badInk" role="alert">
            {error}
          </p>
        )}

        {result && (
          <div
            className={`animate-fade space-y-1.5 rounded-field px-3.5 py-3 ${
              result.is_correct ? "bg-ok/12" : "bg-bad/12"
            }`}
            role="status"
          >
            <p
              className={`flex items-center gap-1.5 text-xs font-medium ${
                result.is_correct ? "text-okInk" : "text-badInk"
              }`}
            >
              {/* Never colour alone: an icon and a word carry the verdict. */}
              <Icon name={result.is_correct ? "check" : "close"} size={15} />
              {result.is_correct ? "درست!" : "پاسخ درست نبود"}
            </p>
            {result.explanation && (
              <p className="text-[12px] leading-6 text-inkMuted">{result.explanation}</p>
            )}
            {earned !== null && (
              <p className="flex items-center gap-1 text-[11px] font-medium text-okInk">
                <Icon name="spark" size={13} />
                <span className="nums">+{toFa(earned)} امتیاز</span>
              </p>
            )}
          </div>
        )}

        {/* --------------------------------------------- source: folded citation
            Always present: a verified card invites the citation open, an
            unverified one says so outright rather than staying silent. */}
        <SourcePanel
          source={source}
          onOpen={() => {
            if (sourceOpened) return;
            setSourceOpened(true);
            onEngagement(item.question_id, Date.now() - enteredAt.current, true);
          }}
        />
      </PaperCard>
    </article>
  );
}

export const ExploreCard = memo(ExploreCardInner);
