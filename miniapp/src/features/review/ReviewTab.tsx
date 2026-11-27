import { useEffect, useMemo, useState } from "react";
import { api, PublicationStatus, ReviewQuestion } from "../../api";
import { formatRelative, toFa } from "../../lib/format";
import { haptic } from "../../lib/telegram";
import { describeError, useAsync } from "../../lib/useAsync";
import { Icon } from "../../ui/Icon";
import {
  Button,
  Chip,
  Group,
  PaperCard,
  PaperOption,
  PaperOptionState,
  Progress,
  ScreenHeader,
} from "../../ui/primitives";
import { EmptyState, ErrorState, InlineNote, ListSkeleton } from "../../ui/states";

/** The server's verdict plus the option this user actually picked. */
interface ShownAnswer {
  is_correct: boolean;
  correct_index: number;
  explanation: string;
  chosen_index: number;
}

const SOURCE_LABELS: Record<string, string> = {
  audio: "صوت",
  voice: "ویس",
  video: "ویدیو",
  pdf: "PDF",
  docx: "Word",
  pptx: "پاورپوینت",
  presentation: "پاورپوینت",
  document: "سند",
  booklet: "جزوه",
  upload: "فایل",
  office: "آفیس",
  text: "متن",
};

function publicationMeta(status: PublicationStatus) {
  switch (status) {
    case "pending":
      return { label: "در صف بررسی", tone: "brand" as const };
    case "approved":
      return { label: "منتشرشده", tone: "ok" as const };
    case "rejected":
      return { label: "ردشده", tone: "bad" as const };
    default:
      return null;
  }
}

/* Options live on paper: ink borders, never the chrome's navy chips. */

function QuestionView({
  question,
  position,
  total,
  answer,
  onAnswer,
  publication,
  onShared,
}: {
  question: ReviewQuestion;
  position: number;
  total: number;
  answer: ShownAnswer | null;
  onAnswer: (chosen: number) => Promise<void>;
  publication: PublicationStatus;
  onShared: (status: string) => void;
}) {
  const [busy, setBusy] = useState(false);
  const [shareBusy, setShareBusy] = useState(false);
  const [shareError, setShareError] = useState<string | null>(null);
  const answered = answer !== null;
  const shown = answer;
  const meta = publicationMeta(publication);

  const pick = async (index: number) => {
    if (answered || busy) return;
    setBusy(true);
    haptic("tap");
    try {
      await onAnswer(index);
      haptic("success");
    } catch (error) {
      haptic("error");
      setShareError(describeError(error) || "ثبت پاسخ انجام نشد؛ دوباره تلاش کنید.");
    } finally {
      setBusy(false);
    }
  };

  const share = async () => {
    setShareBusy(true);
    setShareError(null);
    haptic("tap");
    try {
      const res = await api.shareReviewQuestion(question.question_id);
      onShared(res.status);
      haptic("success");
    } catch (error) {
      haptic("error");
      setShareError(describeError(error) || "ارسال این سؤال انجام نشد.");
    } finally {
      setShareBusy(false);
    }
  };

  return (
    <div className="space-y-3">
      <div className="flex items-center justify-between gap-2 px-1">
        <span className="nums text-[11px] text-faint">
          سؤال {toFa(position)} از {toFa(total)}
        </span>
        <Progress value={position} total={total} label="پیشرفت مرور" />
      </div>

      <PaperCard
        tone={shown ? (shown.is_correct ? "ok" : "bad") : "brand"}
        className="space-y-4"
      >
        <p className="text-[14px] leading-7 text-ink">{question.question}</p>
        <div className="space-y-2">
          {question.options.map((option, index) => {
            let state: PaperOptionState = "idle";
            if (shown) {
              if (index === shown.correct_index) state = "correct";
              else if (index === shown.chosen_index && !shown.is_correct) state = "wrong";
              else state = "muted";
            }
            return (
              <PaperOption
                key={index}
                label={option}
                index={index}
                state={state}
                disabled={answered || busy}
                onPick={() => pick(index)}
              />
            );
          })}
        </div>

        {shown && (
          <div
            className={`space-y-2 rounded-field px-3.5 py-3 ${
              shown.is_correct ? "bg-ok/12" : "bg-bad/12"
            }`}
            role="status"
          >
            <p
              className={`flex items-center gap-1.5 text-xs font-medium ${
                shown.is_correct ? "text-okInk" : "text-badInk"
              }`}
            >
              <Icon name={shown.is_correct ? "check" : "close"} size={15} />
              {shown.is_correct ? "پاسخ درست بود" : "پاسخ نادرست بود"}
              {!shown.is_correct && (
                <span className="font-normal text-inkMuted">
                  · پاسخ درست: گزینهٔ {toFa(shown.correct_index + 1)}
                </span>
              )}
            </p>
            {shown.explanation && (
              <p className="text-[12px] leading-6 text-inkMuted">{shown.explanation}</p>
            )}
          </div>
        )}
      </PaperCard>

      <div className="flex items-center justify-between gap-2">
        {meta ? (
          <Chip tone={meta.tone}>{meta.label}</Chip>
        ) : (
          <span className="text-[11px] text-faint">این سؤال برای خودتان است.</span>
        )}
        {!meta && (
          <Button
            variant="ghost"
            icon="share"
            disabled={shareBusy}
            onClick={share}
            className="mr-auto"
          >
            {shareBusy ? "در حال ارسال…" : "ارسال به یک‌سؤال"}
          </Button>
        )}
      </div>

      {shareError && <InlineNote>{shareError}</InlineNote>}
    </div>
  );
}

function GroupView({
  groupKey,
  title,
  onBack,
}: {
  groupKey: string;
  title: string;
  onBack: () => void;
}) {
  const questions = useAsync(() => api.getReviewQuestions(groupKey), [groupKey]);
  const [index, setIndex] = useState(0);
  const [answers, setAnswers] = useState<Record<number, ShownAnswer>>({});
  const [publications, setPublications] = useState<Record<number, PublicationStatus>>({});

  const list = questions.data?.questions ?? [];

  useEffect(() => {
    setIndex(0);
    setAnswers({});
    setPublications({});
  }, [groupKey]);

  const current = list[index] ?? null;

  const answer = async (chosen: number) => {
    if (!current) return;
    const result = await api.submitReviewAnswer(current.question_id, chosen);
    setAnswers((prev) => ({
      ...prev,
      [current.question_id]: { ...result, chosen_index: chosen },
    }));
  };

  const markShared = (status: string) => {
    if (!current) return;
    const normalised: PublicationStatus =
      status === "approved" || status === "rejected" ? status : "pending";
    setPublications((prev) => ({ ...prev, [current.question_id]: normalised }));
  };

  if (questions.loading && !questions.data) return <ListSkeleton rows={3} />;
  if (questions.error && !questions.data) {
    return (
      <div className="space-y-4">
        <ScreenHeader title={title} onBack={onBack} />
        <ErrorState message={questions.error} onRetry={questions.reload} />
      </div>
    );
  }
  if (!current) {
    return (
      <div className="space-y-4">
        <ScreenHeader title={title} onBack={onBack} />
        <EmptyState emblem="flask" title="سؤالی برای مرور نیست" />
      </div>
    );
  }

  const storedAnswer = answers[current.question_id] ?? null;
  const serverState = current.answered;

  // The server's stored answer is the source of truth; a fresh answer from this
  // session only replaces it once the write has succeeded.
  const resolved: ShownAnswer | null =
    storedAnswer ??
    (serverState
      ? {
          is_correct: serverState.is_correct,
          correct_index: serverState.correct_index,
          explanation: serverState.explanation,
          chosen_index: serverState.chosen_index,
        }
      : null);

  const answeredCount = list.filter(
    (question) => answers[question.question_id] || question.answered,
  ).length;

  return (
    <div className="space-y-4">
      <ScreenHeader
        title={title}
        onBack={onBack}
        subtitle={
          <span className="nums">
            {toFa(answeredCount)} از {toFa(list.length)} سؤال پاسخ داده شده
          </span>
        }
      />

      {questions.data && questions.data.total > list.length && (
        <InlineNote>
          {toFa(questions.data.total - list.length)} سؤال دیگر از این گروه هنوز بارگذاری
          نشده است.
        </InlineNote>
      )}

      <div key={current.question_id} className="animate-fade space-y-3">
        <QuestionView
          question={current}
          position={index + 1}
          total={list.length}
          answer={resolved}
          onAnswer={answer}
          publication={publications[current.question_id] ?? current.publication_status}
          onShared={markShared}
        />
      </div>

      <div className="flex items-center gap-2 pb-1">
        <Button
          icon="back"
          disabled={index === 0}
          onClick={() => {
            haptic("select");
            setIndex((i) => Math.max(0, i - 1));
          }}
          className="flex-1"
        >
          قبلی
        </Button>
        <Button
          variant="primary"
          disabled={index >= list.length - 1}
          onClick={() => {
            haptic("select");
            setIndex((i) => Math.min(list.length - 1, i + 1));
          }}
          className="flex-1"
        >
          بعدی
        </Button>
      </div>
    </div>
  );
}

export function ReviewTab({
  initialPackId,
  onConsumedInitial,
}: {
  initialPackId: number | null;
  onConsumedInitial: () => void;
}) {
  const groups = useAsync(() => api.getReviewGroups(), []);
  const [activeKey, setActiveKey] = useState<string | null>(null);
  const [activeTitle, setActiveTitle] = useState("");

  const list = groups.data?.groups ?? [];

  const totals = useMemo(
    () =>
      list.reduce(
        (acc, group) => ({
          questions: acc.questions + group.question_count,
          answered: acc.answered + group.answered_count,
          correct: acc.correct + group.correct_count,
        }),
        { questions: 0, answered: 0, correct: 0 },
      ),
    [list],
  );

  // A pack opened from the Library lands directly on its review group.
  useEffect(() => {
    if (initialPackId === null || !groups.data) return;
    const key = `pack:${initialPackId}`;
    const group = groups.data.groups.find((item) => item.group_key === key);
    setActiveKey(key);
    setActiveTitle(group?.title ?? "مرور بسته");
    onConsumedInitial();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [initialPackId, groups.data]);

  if (activeKey) {
    return (
      <GroupView
        key={activeKey}
        groupKey={activeKey}
        title={activeTitle}
        onBack={() => {
          setActiveKey(null);
          groups.reload();
        }}
      />
    );
  }

  return (
    <div className="space-y-4">
      <ScreenHeader
        title="مرور"
        subtitle="سؤال‌هایی که از فایل‌های شما ساخته شده‌اند."
      />

      {groups.loading && !groups.data && <ListSkeleton rows={3} />}
      {groups.error && !groups.data && (
        <ErrorState message={groups.error} onRetry={groups.reload} />
      )}

      {groups.data && list.length === 0 && (
        <EmptyState
          emblem="flask"
          title="هنوز سؤالی برای مرور ندارید"
          description="پس از پردازش فایل در ربات، سؤال‌ها به تفکیک بستهٔ مطالعاتی این‌جا گروه‌بندی می‌شوند."
        />
      )}

      {list.length > 0 && (
        <>
          <div className="grid grid-cols-3 divide-x divide-line2 rounded-card border border-line bg-surface">
            <div className="px-4 py-3">
              <p className="text-[11px] text-faint">سؤال‌ها</p>
              <p className="nums mt-1 text-lg font-semibold text-text">
                {toFa(totals.questions)}
              </p>
            </div>
            <div className="px-4 py-3">
              <p className="text-[11px] text-faint">پاسخ‌داده</p>
              <p className="nums mt-1 text-lg font-semibold text-text">
                {toFa(totals.answered)}
              </p>
            </div>
            <div className="px-4 py-3">
              <p className="text-[11px] text-faint">درست</p>
              <p className="nums mt-1 text-lg font-semibold text-ok">
                {toFa(totals.correct)}
              </p>
            </div>
          </div>

          <Group listClassName="stagger">
            {list.map((group, index) => {
              const remaining = Math.max(0, group.question_count - group.answered_count);
              return (
                <button
                  key={group.group_key}
                  type="button"
                  style={{ animationDelay: `${Math.min(index, 6) * 40}ms` }}
                  onClick={() => {
                    haptic("tap");
                    setActiveKey(group.group_key);
                    setActiveTitle(group.title);
                  }}
                  className="pressable w-full space-y-2.5 px-4 py-3.5 text-right"
                >
                  <span className="flex items-start justify-between gap-2">
                    <span className="min-w-0">
                      <span className="block truncate text-sm font-medium text-text">
                        {group.title}
                      </span>
                      <span className="mt-1 flex flex-wrap items-center gap-x-2 gap-y-1 text-[11px] text-faint">
                        <span>{SOURCE_LABELS[group.source_type] ?? group.source_type}</span>
                        <span aria-hidden>·</span>
                        <span className="nums">{toFa(group.question_count)} سؤال</span>
                        <span aria-hidden>·</span>
                        <span>{formatRelative(group.latest_at)}</span>
                      </span>
                    </span>
                    {remaining === 0 ? (
                      <Chip tone="ok" dot>
                        کامل
                      </Chip>
                    ) : (
                      <Chip tone="neutral">{toFa(remaining)} باقی‌مانده</Chip>
                    )}
                  </span>
                  <Progress
                    value={group.answered_count}
                    total={group.question_count}
                    label={`پیشرفت ${group.title}`}
                  />
                </button>
              );
            })}
          </Group>
        </>
      )}
    </div>
  );
}
