import { useEffect, useRef, useState } from "react";
import { api, ExploreAnswerResult, ExploreItem } from "../../api";
import { toFa } from "../../lib/format";
import { haptic } from "../../lib/telegram";
import { useAsync } from "../../lib/useAsync";
import { Icon } from "../../ui/Icon";
import { Button, Card, Chip, ScreenHeader } from "../../ui/primitives";
import { EmptyState, ErrorState, InlineNote, ListSkeleton } from "../../ui/states";

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

const MIN_DWELL_MS = 800;

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

  const recordEngagement = (questionId: number) => {
    const dwell = Date.now() - enteredAt.current;
    if (dwell < MIN_DWELL_MS) return;
    // Fire-and-forget: the feed must never wait on an analytics write.
    void api.sendExploreEngagement(questionId, dwell).catch(() => undefined);
  };

  const advance = () => {
    if (current) recordEngagement(current.question_id);
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
      setError((err as Error).message || "ثبت پاسخ انجام نشد؛ دوباره تلاش کنید.");
    } finally {
      setSubmitting(false);
    }
  };

  if (feed.loading && !feed.data) return <ListSkeleton rows={3} />;
  if (feed.error && !feed.data) {
    return (
      <div className="space-y-4">
        <ScreenHeader title="اکسپلور" />
        <ErrorState message={feed.error} onRetry={feed.reload} />
      </div>
    );
  }
  if (!current) {
    return (
      <div className="space-y-4">
        <ScreenHeader
          title="اکسپلور"
          subtitle="سؤال‌های کوتاه برای مرور سریع در زمان‌های مرده."
        />
        <EmptyState
          icon="explore"
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
        title="اکسپلور"
        subtitle="یک سؤال، یک پاسخ، یک نکتهٔ تازه."
        actions={
          <span className="flex items-center gap-1 rounded-pill border border-line bg-surface px-2.5 py-1 text-[11px] text-muted">
            <Icon name="spark" size={13} className="text-brand" />
            <span className="nums">{toFa(Object.keys(answers).length)} پاسخ</span>
          </span>
        }
      />

      <div key={current.question_id} className="animate-rise space-y-3">
        <Card className="space-y-4">
          <div className="flex items-center justify-between gap-2">
            <div className="flex items-center gap-1.5">
              <Chip tone="brand">{topicLabel(current.topic)}</Chip>
              <Chip>{DIFFICULTY_LABELS[current.difficulty] ?? current.difficulty}</Chip>
            </div>
            {justEarned !== null && (
              <span className="animate-pop flex items-center gap-1 text-[11px] font-medium text-ok">
                <Icon name="spark" size={13} />
                <span className="nums">+{toFa(justEarned)} امتیاز</span>
              </span>
            )}
          </div>

          <p className="text-[15px] leading-8 text-text">{current.question_text}</p>

          <div className="space-y-2">
            {current.options.map((option, optionIndex) => {
              let tone = "border-line bg-surface2 text-text hover:bg-surface3";
              let disabled = false;
              if (result) {
                disabled = true;
                if (optionIndex === result.correct_index) {
                  tone = "border-ok/50 bg-okSoft text-text";
                } else if (optionIndex === pickedOption && !result.is_correct) {
                  tone = "border-bad/50 bg-badSoft text-text";
                } else {
                  tone = "border-line bg-surface text-muted opacity-70";
                }
              }
              return (
                <button
                  key={optionIndex}
                  type="button"
                  disabled={disabled || (submitting && !result)}
                  onClick={() => answer(optionIndex)}
                  className={`pressable flex w-full items-start gap-3 rounded-field border px-3.5 py-3 text-right text-[13px] leading-6 transition-colors ${tone}`}
                >
                  <span
                    className={`mt-0.5 flex h-5 w-5 shrink-0 items-center justify-center rounded-full border text-[10px] ${
                      result && optionIndex === result.correct_index
                        ? "border-ok bg-ok text-bg"
                        : result && optionIndex === pickedOption
                          ? "border-bad bg-bad text-bg"
                          : "border-line text-faint"
                    }`}
                    aria-hidden
                  >
                    {result && optionIndex === result.correct_index ? (
                      <Icon name="check" size={12} strokeWidth={3} />
                    ) : (
                      toFa(optionIndex + 1)
                    )}
                  </span>
                  <span className="flex-1">{option}</span>
                </button>
              );
            })}
          </div>

          {submitting && !result && (
            <p className="text-center text-[11px] text-faint">در حال ثبت پاسخ…</p>
          )}

          {error && <InlineNote>{error}</InlineNote>}

          {result && (
            <div
              className={`animate-fade space-y-1.5 rounded-field px-3.5 py-3 ${
                result.is_correct ? "bg-okSoft" : "bg-badSoft"
              }`}
              role="status"
            >
              <p
                className={`flex items-center gap-1.5 text-xs font-medium ${
                  result.is_correct ? "text-ok" : "text-bad"
                }`}
              >
                <Icon name={result.is_correct ? "check" : "close"} size={15} />
                {result.is_correct ? "درست!" : "پاسخ درست نبود"}
              </p>
              {result.explanation && (
                <p className="text-[12px] leading-6 text-muted">{result.explanation}</p>
              )}
            </div>
          )}
        </Card>
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
