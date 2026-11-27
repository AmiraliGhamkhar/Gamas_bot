import { useEffect, useRef, useState } from "react";
import { api, ExploreAnswerResult, ExploreItem } from "../../api";
import { toFa } from "../../lib/format";
import { haptic } from "../../lib/telegram";
import { describeError, useAsync } from "../../lib/useAsync";
import { EcgLoader } from "../../ui/Ecg";
import { Icon } from "../../ui/Icon";
import {
  Button,
  Chip,
  PaperCard,
  PaperOption,
  PaperOptionState,
  ScreenHeader,
} from "../../ui/primitives";
import { EmptyState, ErrorState, ListSkeleton } from "../../ui/states";

const TOPIC_LABELS: Record<string, string> = {
  biology: "زیست‌شناسی",
  chemistry: "شیمی",
  physics: "فیزیک",
  math: "ریاضی",
  literature: "ادبیات",
  arabic: "عربی",
  language: "زبان",
  english: "انگلیسی",
  history: "تاریخ",
  geography: "جغرافیا",
  religion: "دینی",
  psychology: "روان‌شناسی",
  economics: "اقتصاد",
  law: "حقوق",
  accounting: "حسابداری",
  statistics: "آمار",
  computer: "کامپیوتر",
  art: "هنر",
  philosophy: "فلسفه",
  civics: "اجتماعی",
};

function topicLabel(topic: string): string {
  return TOPIC_LABELS[topic] ?? topic;
}

const DIFFICULTY_LABELS: Record<string, string> = {
  easy: "آسان",
  medium: "متوسط",
  hard: "دشوار",
};

export function ExploreTab({ onReward }: { onReward: () => void }) {
  const feed = useAsync(api.getExploreFeed, []);
  const [index, setIndex] = useState(0);
  const [answers, setAnswers] = useState<Record<number, ExploreAnswerResult>>({});
  // The option the user picked, kept per question so the wrong choice can stay
  // highlighted after the server's result arrives.
  const [chosen, setChosen] = useState<Record<number, number>>({});
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [justEarned, setJustEarned] = useState<number | null>(null);

  const enteredAt = useRef<number>(Date.now());

  const items = feed.data?.items ?? [];
  const current: ExploreItem | null = items[index] ?? null;

  // Dwell is measured per card from the moment it becomes the visible one.
  useEffect(() => {
    enteredAt.current = Date.now();
    setError(null);
    setJustEarned(null);
  }, [current?.question_id]);

  // A skipped card must still leave an impression — the server's seen-filter
  // and the recommender's bounce signal both depend on it — so this fires on
  // every skip regardless of dwell. An answered card already recorded its
  // impression inside `submitExploreAnswer` and is never sent twice.
  const recordEngagement = (questionId: number) => {
    const dwell = Math.max(0, Date.now() - enteredAt.current);
    // Fire-and-forget: the feed must never wait on an analytics write.
    void api.sendExploreEngagement(questionId, dwell).catch(() => undefined);
  };

  const advance = () => {
    if (current && !answers[current.question_id]) recordEngagement(current.question_id);
    haptic("select");
    setIndex((i) => (i + 1 >= items.length ? i : i + 1));
  };

  // Seen questions are filtered server-side, so a reload is a new round.
  const startNewRound = () => {
    setIndex(0);
    setAnswers({});
    setChosen({});
    haptic("tap");
    feed.reload();
  };

  const answer = async (optionIndex: number) => {
    if (!current || submitting || answers[current.question_id]) return;
    setSubmitting(true);
    setChosen((prev) => ({ ...prev, [current.question_id]: optionIndex }));
    setError(null);
    const dwell = Date.now() - enteredAt.current;
    try {
      const res = await api.submitExploreAnswer(current.question_id, optionIndex, dwell);
      setAnswers((prev) => ({ ...prev, [current.question_id]: res }));
      haptic(res.is_correct ? "success" : "error");
      if (res.points_awarded > 0) {
        setJustEarned(res.points_awarded);
        onReward();
      }
    } catch (err) {
      haptic("error");
      setError(describeError(err) || "ثبت پاسخ انجام نشد؛ دوباره تلاش کنید.");
    } finally {
      setSubmitting(false);
    }
  };

  if (feed.loading && !feed.data) return <ListSkeleton rows={3} />;
  if (feed.error && !feed.data) {
    return (
      <div className="space-y-4">
        <ScreenHeader title="یک‌سؤال" />
        <ErrorState message={feed.error} onRetry={feed.reload} />
      </div>
    );
  }
  if (!current) {
    return (
      <div className="space-y-4">
        <ScreenHeader
          title="یک‌سؤال"
          subtitle="سؤال‌های کوتاه برای مرور سریع در زمان‌های مرده."
        />
        <EmptyState
          emblem="network"
          title="فعلاً سؤالی برای نمایش نیست"
          description="با پاسخ دادن به سؤال‌های دیگر یا انتشار سؤال در بخش مرور، این فهرست پر می‌شود."
          action={
            <Button variant="ghost" icon="refresh" onClick={feed.reload}>
              بررسی دوباره
            </Button>
          }
        />
      </div>
    );
  }

  const result = answers[current.question_id] ?? null;
  const pickedOption = chosen[current.question_id];
  const atEnd = index >= items.length - 1;
  return (
    <div className="space-y-4">
      <ScreenHeader
        title="یک‌سؤال"
        subtitle="یک سؤال، یک پاسخ، یک نکتهٔ تازه."
        actions={
          <span className="flex items-center gap-1 rounded-pill border border-line bg-surface px-2.5 py-1 text-[11px] text-muted">
            <Icon name="spark" size={13} className="text-brand" />
            <span className="nums">{toFa(Object.keys(answers).length)} پاسخ</span>
          </span>
        }
      />

      <div key={current.question_id} className="animate-rise space-y-3">
        <PaperCard tone={result ? (result.is_correct ? "ok" : "bad") : "brand"} className="space-y-4">
          <div className="flex items-center justify-between gap-2">
            <div className="flex min-w-0 items-center gap-1.5">
              <Chip tone="brand" onPaper className="max-w-[16ch] overflow-hidden">
                <span className="truncate">{topicLabel(current.topic)}</span>
              </Chip>
              <Chip onPaper>{DIFFICULTY_LABELS[current.difficulty] ?? current.difficulty}</Chip>
            </div>
            {justEarned !== null && (
              /* The reward lands quietly: a teal line of text, not a coin jingle. */
              <span className="animate-fade flex items-center gap-1 text-[11px] font-medium text-okInk">
                <Icon name="spark" size={13} />
                <span className="nums">+{toFa(justEarned)} امتیاز</span>
              </span>
            )}
          </div>

          <p className="text-[15px] leading-8 text-ink">{current.question_text}</p>

          <div className="space-y-2">
            {current.options.map((option, optionIndex) => {
              let state: PaperOptionState = "idle";
              if (result) {
                if (optionIndex === result.correct_index) state = "correct";
                else if (optionIndex === pickedOption && !result.is_correct) state = "wrong";
                else state = "muted";
              }
              return (
                <PaperOption
                  key={optionIndex}
                  label={option}
                  index={optionIndex}
                  state={state}
                  disabled={Boolean(result) || submitting}
                  onPick={() => answer(optionIndex)}
                />
              );
            })}
          </div>

          {submitting && !result && (
            <div className="flex flex-col items-center gap-1.5 py-1">
              <EcgLoader label="در حال ثبت پاسخ" className="text-trace" />
              <p className="text-[11px] text-inkMuted">در حال ثبت پاسخ…</p>
            </div>
          )}

          {error && (
            <p className="rounded-field bg-bad/12 px-3 py-2 text-[11px] leading-6 text-badInk">
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
                <Icon name={result.is_correct ? "check" : "close"} size={15} />
                {result.is_correct ? "درست!" : "پاسخ درست نبود"}
              </p>
              {result.explanation && (
                <p className="text-[12px] leading-6 text-inkMuted">{result.explanation}</p>
              )}
            </div>
          )}
        </PaperCard>
      </div>

      <div className="flex items-center gap-2 pb-1">
        {!result ? (
          <Button
            variant="ghost"
            block
            onClick={advance}
            disabled={atEnd}
            className="!text-faint"
          >
            رد کردن
          </Button>
        ) : (
          <Button variant="primary" block icon="forward" onClick={advance} disabled={atEnd}>
            سؤال بعدی
          </Button>
        )}
      </div>

      {atEnd && (
        <div className="space-y-2 pb-2 text-center">
          <p className="text-[11px] leading-5 text-faint">
            {result
              ? "به پایان سؤال‌های این دور رسیدید."
              : "سؤال پاسخ‌داده‌نشده‌ای در این دور نمانده است."}
          </p>
          <Button variant="secondary" icon="refresh" onClick={startNewRound}>
            سؤال‌های تازه
          </Button>
        </div>
      )}
    </div>
  );
}
