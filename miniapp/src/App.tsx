import { useEffect, useRef, useState } from "react";
import {
  api,
  ExploreAnswerResult,
  ExploreItem,
  MeResponse,
  PaymentItem,
  ReferralsResponse,
  ReviewAnswerResult,
  ReviewGroup,
  ReviewQuestion,
  RewardCatalogItem,
  StudyAsset,
  StudyPack,
  SubscriptionResponse,
} from "./api";

type Tab = "account" | "library" | "review" | "explore";

interface TgDisplayUser {
  first_name?: string;
  last_name?: string;
  username?: string;
  photo_url?: string;
}

function getTgDisplayUser(): TgDisplayUser {
  const tg = (
    window as unknown as {
      Telegram?: { WebApp?: { initDataUnsafe?: { user?: TgDisplayUser } } };
    }
  ).Telegram;
  return tg?.WebApp?.initDataUnsafe?.user ?? {};
}

function MyAccountTab() {
  const [me, setMe] = useState<MeResponse | null>(null);
  const [sub, setSub] = useState<SubscriptionResponse | null>(null);
  const [refs, setRefs] = useState<ReferralsResponse | null>(null);
  const [payments, setPayments] = useState<PaymentItem[]>([]);
  const [catalog, setCatalog] = useState<RewardCatalogItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);
  const [redeemMsg, setRedeemMsg] = useState<string | null>(null);

  const loadAccount = () => {
    setLoading(true);
    setError(null);
    Promise.all([
      api.getMe(),
      api.getSubscription(),
      api.getReferrals(),
      api.getPayments(),
      api.getRewardCatalog(),
    ])
      .then(([meRes, subRes, refRes, payRes, catRes]) => {
        setMe(meRes);
        setSub(subRes);
        setRefs(refRes);
        setPayments(payRes.payments);
        setCatalog(catRes.catalog);
      })
      .catch((e: Error) => setError(e.message))
      .finally(() => setLoading(false));
  };

  useEffect(() => {
    loadAccount();
  }, []);

  if (loading) {
    return <div className="p-4 text-sm text-slate-400">در حال بارگذاری حساب…</div>;
  }

  if (error || !me) {
    return (
      <div className="space-y-3 rounded-2xl border border-rose-800 bg-rose-950/40 p-4 text-sm text-rose-200">
        <div>خطا در دریافت اطلاعات حساب: {error || "نامشخص"}</div>
        <button
          onClick={loadAccount}
          className="rounded-xl bg-rose-700 px-3 py-1.5 text-xs font-medium text-white"
        >
          تلاش مجدد
        </button>
      </div>
    );
  }

  const tgUser = getTgDisplayUser();
  const fullName =
    [tgUser.first_name, tgUser.last_name].filter(Boolean).join(" ") || "کاربر گاماس";
  const initials = fullName.slice(0, 1);
  const minutes = Math.floor(me.credit_seconds / 60);
  const referralLink = `https://t.me/Gamas_bot?start=ref_${me.user_id}`;

  const handleCopyRef = async () => {
    try {
      await navigator.clipboard.writeText(referralLink);
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    } catch {
      setCopied(false);
    }
  };

  const handleRedeem = async (code: string) => {
    setRedeemMsg(null);
    try {
      const res = await api.redeemReward(code);
      setMe((prev) => (prev ? { ...prev, reward_points: res.balance } : prev));
      setRedeemMsg("تبدیل امتیاز با موفقیت انجام شد.");
    } catch (e) {
      setRedeemMsg((e as Error).message);
    }
  };

  return (
    <div className="space-y-4">
      {/* Profile Header */}
      <div className="flex items-center gap-3 rounded-2xl border border-slate-800 bg-slate-900 p-4">
        {tgUser.photo_url ? (
          <img
            src={tgUser.photo_url}
            alt={fullName}
            className="h-12 w-12 rounded-full object-cover"
          />
        ) : (
          <div className="flex h-12 w-12 items-center justify-center rounded-full bg-sky-600/30 text-lg font-bold text-sky-300">
            {initials}
          </div>
        )}
        <div className="min-w-0 flex-1">
          <div className="truncate text-base font-bold">{fullName}</div>
          {tgUser.username && (
            <div className="truncate text-xs text-slate-400">@{tgUser.username}</div>
          )}
          <div className="text-xs text-slate-500">شناسه: {me.user_id}</div>
        </div>
      </div>

      {/* Credits & Rewards */}
      <div className="grid grid-cols-2 gap-3">
        <div className="rounded-2xl border border-slate-800 bg-slate-900 p-4">
          <div className="text-xs text-slate-400">اعتبار گاماس</div>
          <div className="mt-1 text-xl font-bold text-emerald-400">
            {minutes} دقیقه
          </div>
          <div className="mt-1 text-[11px] text-slate-500">
            آزمون رایگان: {me.free_uses?.quiz ? "فعال" : "استفاده شده"}
          </div>
        </div>
        <div className="rounded-2xl border border-slate-800 bg-slate-900 p-4">
          <div className="text-xs text-slate-400">امتیاز پاداش</div>
          <div className="mt-1 text-xl font-bold text-amber-400">
            {me.reward_points}
          </div>
          <div className="mt-1 text-[11px] text-slate-500">جدا از اعتبار</div>
        </div>
      </div>

      {/* Subscription */}
      <div className="rounded-2xl border border-slate-800 bg-slate-900 p-4 space-y-1">
        <div className="text-sm font-semibold">اشتراک و دسترسی</div>
        <div className="text-xs text-slate-400">
          {sub?.required_channel
            ? `کانال عضویت: ${sub.required_channel}`
            : "بدون محدودیت کانال"}
        </div>
      </div>

      {/* Referrals */}
      <div className="rounded-2xl border border-slate-800 bg-slate-900 p-4 space-y-2">
        <div className="flex items-center justify-between">
          <span className="text-sm font-semibold">دعوت از دوستان</span>
          <span className="text-xs text-sky-400">
            {refs?.invited_count ?? 0} دعوت موفق
          </span>
        </div>
        <div className="flex gap-2">
          <button
            onClick={handleCopyRef}
            className="flex-1 rounded-xl bg-slate-800 px-3 py-2 text-xs font-medium text-slate-200"
          >
            {copied ? "کپی شد" : "کپی لینک دعوت"}
          </button>
          <a
            href={`https://t.me/share/url?url=${encodeURIComponent(referralLink)}`}
            target="_blank"
            rel="noreferrer"
            className="flex-1 rounded-xl bg-sky-600 px-3 py-2 text-center text-xs font-medium text-white"
          >
            اشتراک‌گذاری
          </a>
        </div>
      </div>

      {/* Rewards Catalog */}
      <div className="rounded-2xl border border-slate-800 bg-slate-900 p-4 space-y-2">
        <div className="text-sm font-semibold">تبدیل امتیاز پاداش</div>
        {redeemMsg && <div className="text-xs text-amber-300">{redeemMsg}</div>}
        {catalog.length === 0 ? (
          <div className="text-xs text-slate-400">موردی موجود نیست.</div>
        ) : (
          <div className="space-y-2">
            {catalog.map((item) => (
              <div
                key={item.code}
                className="flex items-center justify-between rounded-xl border border-slate-800 bg-slate-950/60 p-2.5 text-xs"
              >
                <div>
                  <div className="font-medium">{item.title}</div>
                  <div className="text-slate-400">{item.cost_points} امتیاز</div>
                </div>
                <button
                  onClick={() => handleRedeem(item.code)}
                  disabled={me.reward_points < item.cost_points}
                  className="rounded-lg bg-amber-600 px-3 py-1.5 font-medium text-white disabled:opacity-40"
                >
                  دریافت
                </button>
              </div>
            ))}
          </div>
        )}
      </div>

      {/* Payments */}
      <div className="rounded-2xl border border-slate-800 bg-slate-900 p-4 space-y-2">
        <div className="text-sm font-semibold">سوابق پرداخت</div>
        {payments.length === 0 ? (
          <div className="text-xs text-slate-400">تراکنشی ثبت نشده است.</div>
        ) : (
          <div className="space-y-2">
            {payments.map((p) => (
              <div
                key={p.payment_id}
                className="flex items-center justify-between rounded-xl border border-slate-800 bg-slate-950/60 p-2.5 text-xs"
              >
                <div>
                  <div className="font-medium">
                    {p.amount_toman.toLocaleString()} تومان
                  </div>
                  <div className="text-slate-500">
                    {p.created_at.slice(0, 10)}
                  </div>
                </div>
                <span className="rounded-md bg-slate-800 px-2 py-0.5 text-[11px] text-slate-300">
                  {p.status}
                </span>
              </div>
            ))}
          </div>
        )}
      </div>

      {/* Support */}
      <a
        href="https://t.me/Gamas_bot?start=support"
        target="_blank"
        rel="noreferrer"
        className="block rounded-2xl border border-slate-800 bg-slate-900 p-3 text-center text-xs font-medium text-sky-400"
      >
        پشتیبانی در ربات تلگرام
      </a>
    </div>
  );
}

function LibraryTab({ onOpenReview }: { onOpenReview: () => void }) {
  const [packs, setPacks] = useState<StudyPack[]>([]);
  const [selected, setSelected] = useState<{
    pack: StudyPack;
    assets: StudyAsset[];
  } | null>(null);
  const [search, setSearch] = useState("");
  const [sortOrder, setSortOrder] = useState<"newest" | "oldest">("newest");
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const load = () => {
    setLoading(true);
    setError(null);
    api
      .listPacks()
      .then((res) => setPacks(res.packs))
      .catch((e: Error) => setError(e.message))
      .finally(() => setLoading(false));
  };

  useEffect(() => {
    load();
  }, []);

  const openPack = async (packId: number) => {
    setError(null);
    try {
      const detail = await api.getPack(packId);
      setSelected(detail);
    } catch (err) {
      setError((err as Error).message);
    }
  };

  const handleDelete = async (packId: number) => {
    try {
      await api.deletePack(packId);
      setSelected(null);
      load();
    } catch (err) {
      setError((err as Error).message);
    }
  };

  if (selected) {
    return (
      <div className="space-y-4">
        <div className="flex items-center justify-between">
          <button
            onClick={() => setSelected(null)}
            className="text-sm text-sky-400"
          >
            بازگشت به کتابخانه
          </button>
          <button
            onClick={() => handleDelete(selected.pack.pack_id)}
            className="text-sm text-rose-400"
          >
            حذف بسته
          </button>
        </div>

        <div className="rounded-2xl border border-slate-800 bg-slate-900 p-4 space-y-1">
          <h2 className="text-lg font-bold">{selected.pack.title}</h2>
          <div className="flex flex-wrap gap-2 text-xs text-slate-400">
            <span>نوع: {selected.pack.source_type}</span>
            <span>•</span>
            <span>وضعیت: {selected.pack.status}</span>
            <span>•</span>
            <span>{selected.pack.created_at.slice(0, 10)}</span>
          </div>
        </div>

        <button
          onClick={onOpenReview}
          className="w-full rounded-xl bg-emerald-600 py-2.5 text-sm font-medium text-white"
        >
          مرور سؤالات این بسته
        </button>

        <div className="space-y-2">
          {selected.assets.length === 0 ? (
            <div className="rounded-xl border border-slate-800 bg-slate-900 p-4 text-sm text-slate-400">
              هنوز فایلی در این بسته ثبت نشده است.
            </div>
          ) : (
            selected.assets.map((a) => (
              <div
                key={a.asset_id}
                className="flex items-center justify-between rounded-xl border border-slate-800 bg-slate-900 p-3 text-sm"
              >
                <div>
                  <div className="font-medium">{a.title}</div>
                  <div className="text-xs text-slate-400">
                    {a.kind} • {Math.max(1, Math.round(a.byte_size / 1024))} KB
                  </div>
                </div>
                <a
                  href={api.assetDownloadUrl(selected.pack.pack_id, a.asset_id)}
                  className="rounded-lg bg-sky-600 px-3 py-1.5 text-xs font-medium text-white"
                >
                  دانلود
                </a>
              </div>
            ))
          )}
        </div>
      </div>
    );
  }

  const filtered = packs
    .filter((p) => {
      const q = search.trim().toLowerCase();
      if (!q) return true;
      return (
        p.title.toLowerCase().includes(q) ||
        p.source_type.toLowerCase().includes(q)
      );
    })
    .sort((a, b) => {
      const cmp = a.created_at.localeCompare(b.created_at);
      return sortOrder === "newest" ? -cmp : cmp;
    });

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <h1 className="text-lg font-bold">کتابخانه</h1>
        <button
          onClick={() =>
            setSortOrder((s) => (s === "newest" ? "oldest" : "newest"))
          }
          className="rounded-lg border border-slate-800 bg-slate-900 px-2.5 py-1 text-xs text-slate-300"
        >
          {sortOrder === "newest" ? "جدیدترین" : "قدیمی‌ترین"}
        </button>
      </div>

      <input
        value={search}
        onChange={(e) => setSearch(e.target.value)}
        placeholder="جستجو در بسته‌های مطالعاتی…"
        className="w-full rounded-xl border border-slate-800 bg-slate-900 px-3 py-2 text-sm text-slate-100 placeholder-slate-500 focus:border-sky-500 focus:outline-none"
      />

      {loading && (
        <div className="p-4 text-sm text-slate-400">در حال بارگذاری…</div>
      )}

      {error && (
        <div className="space-y-2 rounded-xl border border-rose-800 bg-rose-950/40 p-3 text-xs text-rose-200">
          <div>{error}</div>
          <button
            onClick={load}
            className="rounded-lg bg-rose-700 px-3 py-1 text-white"
          >
            تلاش مجدد
          </button>
        </div>
      )}

      {!loading && !error && (
        <div className="space-y-2">
          {filtered.length === 0 ? (
            <div className="rounded-xl border border-slate-800 bg-slate-900 p-4 text-sm text-slate-400">
              {packs.length === 0
                ? "کتابخانه شما خالی است. یک فایل در ربات بفرستید."
                : "موردی یافت نشد."}
            </div>
          ) : (
            filtered.map((p) => (
              <button
                key={p.pack_id}
                onClick={() => openPack(p.pack_id)}
                className="flex w-full items-center justify-between rounded-2xl border border-slate-800 bg-slate-900 p-4 text-right"
              >
                <div className="min-w-0 flex-1 space-y-1">
                  <div className="truncate font-medium">{p.title}</div>
                  <div className="flex items-center gap-2 text-xs text-slate-400">
                    <span>{p.source_type}</span>
                    <span>•</span>
                    <span>{p.created_at.slice(0, 10)}</span>
                    <span>•</span>
                    <span>{p.asset_count ?? 0} فایل</span>
                  </div>
                </div>
                <span className="mr-2 rounded-md bg-slate-800 px-2 py-0.5 text-[11px] text-slate-300">
                  {p.status}
                </span>
              </button>
            ))
          )}
        </div>
      )}
    </div>
  );
}

function ReviewTab() {
  const [groups, setGroups] = useState<ReviewGroup[]>([]);
  const [questions, setQuestions] = useState<ReviewQuestion[]>([]);
  const [activeSession, setActiveSession] = useState<number | null>(null);
  const [answers, setAnswers] = useState<Record<number, ReviewAnswerResult>>({});
  const [selectedIds, setSelectedIds] = useState<number[]>([]);
  const [confirmShare, setConfirmShare] = useState(false);
  const [shareMsg, setShareMsg] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const load = () => {
    setLoading(true);
    setError(null);
    Promise.all([api.getReviewGroups(), api.getReviewQuestions()])
      .then(([gRes, qRes]) => {
        setGroups(gRes.groups);
        setQuestions(qRes.questions);
      })
      .catch((e: Error) => setError(e.message))
      .finally(() => setLoading(false));
  };

  useEffect(() => {
    load();
  }, []);

  const handleAnswer = async (qid: number, chosenIdx: number) => {
    try {
      const res = await api.submitReviewAnswer(qid, chosenIdx);
      setAnswers((prev) => ({ ...prev, [qid]: res }));
    } catch (e) {
      setError((e as Error).message);
    }
  };

  const toggleSelect = (qid: number) => {
    setSelectedIds((prev) =>
      prev.includes(qid) ? prev.filter((id) => id !== qid) : [...prev, qid]
    );
  };

  const handleShare = async () => {
    setShareMsg(null);
    try {
      await Promise.all(selectedIds.map((id) => api.shareReviewQuestion(id)));
      setSelectedIds([]);
      setConfirmShare(false);
      setShareMsg("سؤالات انتخاب‌شده برای بررسی و انتشار به صف کاوش ارسال شدند.");
    } catch (e) {
      setError((e as Error).message);
    }
  };

  if (loading) {
    return <div className="p-4 text-sm text-slate-400">در حال بارگذاری مرور…</div>;
  }

  if (error) {
    return (
      <div className="space-y-2 rounded-2xl border border-rose-800 bg-rose-950/40 p-4 text-xs text-rose-200">
        <div>{error}</div>
        <button onClick={load} className="rounded-lg bg-rose-700 px-3 py-1 text-white">
          تلاش مجدد
        </button>
      </div>
    );
  }

  if (activeSession !== null) {
    const sessionQs = questions.filter((q) => q.session_id === activeSession);
    const answeredCount = sessionQs.filter((q) => answers[q.question_id]).length;
    const correctCount = sessionQs.filter((q) => answers[q.question_id]?.is_correct).length;

    return (
      <div className="space-y-4">
        <div className="flex items-center justify-between">
          <button
            onClick={() => setActiveSession(null)}
            className="text-sm text-sky-400"
          >
            بازگشت به بسته‌ها
          </button>
          <span className="text-xs text-slate-400">
            پاسخ: {answeredCount}/{sessionQs.length} • صحیح: {correctCount}
          </span>
        </div>

        {shareMsg && (
          <div className="rounded-xl border border-emerald-800 bg-emerald-950/40 p-3 text-xs text-emerald-200">
            {shareMsg}
          </div>
        )}

        {selectedIds.length > 0 && (
          <div className="rounded-xl border border-sky-800 bg-sky-950/40 p-3 space-y-2">
            {!confirmShare ? (
              <button
                onClick={() => setConfirmShare(true)}
                className="w-full rounded-lg bg-sky-600 py-2 text-xs font-medium text-white"
              >
                اشتراک‌گذاری {selectedIds.length} سؤال انتخاب‌شده در کاوش
              </button>
            ) : (
              <div className="flex gap-2">
                <button
                  onClick={handleShare}
                  className="flex-1 rounded-lg bg-emerald-600 py-2 text-xs font-medium text-white"
                >
                  تأیید ارسال برای بررسی
                </button>
                <button
                  onClick={() => setConfirmShare(false)}
                  className="flex-1 rounded-lg bg-slate-800 py-2 text-xs text-slate-300"
                >
                  انصراف
                </button>
              </div>
            )}
          </div>
        )}

        <div className="space-y-3">
          {sessionQs.map((q, idx) => {
            const res = answers[q.question_id];
            const isSelected = selectedIds.includes(q.question_id);
            return (
              <div
                key={q.question_id}
                className="rounded-2xl border border-slate-800 bg-slate-900 p-4 space-y-3"
              >
                <div className="flex items-start justify-between gap-2">
                  <div className="text-sm font-medium">
                    {idx + 1}. {q.question}
                  </div>
                  <input
                    type="checkbox"
                    checked={isSelected}
                    onChange={() => toggleSelect(q.question_id)}
                    aria-label="انتخاب برای اشتراک‌گذاری"
                    className="mt-1 h-4 w-4 accent-sky-500"
                  />
                </div>
                <div className="space-y-1.5">
                  {q.options.map((opt, oIdx) => {
                    let cls = "border-slate-800 bg-slate-950/60 text-slate-200";
                    if (res) {
                      if (oIdx === res.correct_index) {
                        cls = "border-emerald-700 bg-emerald-950/50 text-emerald-200";
                      } else {
                        cls = "border-slate-800 bg-slate-950/30 text-slate-500";
                      }
                    }
                    return (
                      <button
                        key={oIdx}
                        onClick={() => handleAnswer(q.question_id, oIdx)}
                        className={`w-full rounded-xl border px-3 py-2 text-right text-xs ${cls}`}
                      >
                        {opt}
                      </button>
                    );
                  })}
                </div>
                {res && res.explanation && (
                  <div className="rounded-xl bg-slate-950/80 p-2.5 text-xs text-slate-300">
                    {res.explanation}
                  </div>
                )}
              </div>
            );
          })}
        </div>
      </div>
    );
  }

  return (
    <div className="space-y-4">
      <h1 className="text-lg font-bold">مرور</h1>
      {groups.length === 0 ? (
        <div className="rounded-xl border border-slate-800 bg-slate-900 p-4 text-sm text-slate-400">
          هنوز آزمونی برای مرور وجود ندارد.
        </div>
      ) : (
        <div className="space-y-2">
          {groups.map((g) => (
            <button
              key={g.session_id}
              onClick={() => setActiveSession(g.session_id)}
              className="flex w-full items-center justify-between rounded-2xl border border-slate-800 bg-slate-900 p-4 text-right"
            >
              <div>
                <div className="font-medium">بسته آزمون #{g.session_id}</div>
                <div className="text-xs text-slate-400">
                  {g.question_count} سؤال • {g.created_at.slice(0, 10)}
                </div>
              </div>
              <span className="text-xs text-sky-400">شروع مرور</span>
            </button>
          ))}
        </div>
      )}
    </div>
  );
}

function ExploreCard({
  item,
  feedback,
  onAnswer,
  onLeave,
}: {
  item: ExploreItem;
  feedback: ExploreAnswerResult | undefined;
  onAnswer: (questionId: number, chosenIdx: number, activeMs: number) => void;
  onLeave: (questionId: number, activeMs: number) => void;
}) {
  const cardRef = useRef<HTMLDivElement | null>(null);
  const activeMsRef = useRef(0);
  const visibleSinceRef = useRef<number | null>(null);
  const isVisibleRef = useRef(false);
  const emittedRef = useRef(false);

  const pauseTimer = () => {
    if (visibleSinceRef.current !== null) {
      activeMsRef.current += Math.max(0, Date.now() - visibleSinceRef.current);
      visibleSinceRef.current = null;
    }
  };

  const resumeTimer = () => {
    if (
      isVisibleRef.current &&
      document.visibilityState === "visible" &&
      document.hasFocus() &&
      visibleSinceRef.current === null
    ) {
      visibleSinceRef.current = Date.now();
    }
  };

  useEffect(() => {
    const el = cardRef.current;
    if (!el) return;

    const observer = new IntersectionObserver(
      (entries) => {
        const entry = entries[0];
        if (!entry) return;
        const nowVisible = entry.isIntersecting && entry.intersectionRatio >= 0.5;
        if (nowVisible && !isVisibleRef.current) {
          isVisibleRef.current = true;
          resumeTimer();
        } else if (!nowVisible && isVisibleRef.current) {
          isVisibleRef.current = false;
          pauseTimer();
          if (!emittedRef.current && activeMsRef.current > 0) {
            emittedRef.current = true;
            onLeave(item.question_id, activeMsRef.current);
          }
        }
      },
      { threshold: [0.5] }
    );
    observer.observe(el);

    const onVis = () => {
      if (document.visibilityState === "visible") resumeTimer();
      else pauseTimer();
    };
    const onBlur = () => pauseTimer();
    const onFocus = () => resumeTimer();

    document.addEventListener("visibilitychange", onVis);
    window.addEventListener("blur", onBlur);
    window.addEventListener("focus", onFocus);

    return () => {
      observer.disconnect();
      document.removeEventListener("visibilitychange", onVis);
      window.removeEventListener("blur", onBlur);
      window.removeEventListener("focus", onFocus);
      pauseTimer();
    };
  }, [item.question_id]);

  const handleSelect = (idx: number) => {
    if (feedback) return;
    pauseTimer();
    emittedRef.current = true;
    onAnswer(item.question_id, idx, activeMsRef.current);
  };

  return (
    <div
      ref={cardRef}
      className="snap-start min-h-[calc(100vh-7rem)] flex flex-col justify-between rounded-3xl border border-slate-800 bg-slate-900 p-5"
    >
      <div className="space-y-4">
        <span className="inline-block rounded-full bg-sky-500/15 px-3 py-1 text-xs font-medium text-sky-300">
          {item.topic}
        </span>
        <div className="text-base font-semibold leading-relaxed">
          {item.question_text}
        </div>
        <div className="space-y-2 pt-2">
          {item.options.map((opt, idx) => {
            let cls = "border-slate-800 bg-slate-950/60 text-slate-200";
            if (feedback) {
              if (idx === feedback.correct_index) {
                cls = "border-emerald-600 bg-emerald-950/60 text-emerald-200";
              } else {
                cls = "border-slate-800 bg-slate-950/30 text-slate-500";
              }
            }
            return (
              <button
                key={idx}
                onClick={() => handleSelect(idx)}
                disabled={Boolean(feedback)}
                className={`w-full rounded-2xl border px-4 py-3 text-right text-sm ${cls}`}
              >
                {opt}
              </button>
            );
          })}
        </div>
        {feedback && (
          <div className="space-y-1 rounded-2xl bg-slate-950/80 p-3 text-xs">
            <div
              className={
                feedback.is_correct
                  ? "text-emerald-400 font-semibold"
                  : "text-rose-400 font-semibold"
              }
            >
              {feedback.is_correct ? "پاسخ صحیح!" : "پاسخ نادرست"}
              {feedback.points_awarded > 0 &&
                ` (+${feedback.points_awarded} امتیاز پاداش)`}
            </div>
            {feedback.explanation && (
              <div className="text-slate-300">{feedback.explanation}</div>
            )}
          </div>
        )}
      </div>
      <div className="pt-4 text-center text-[11px] text-slate-500">
        برای سؤال بعدی به پایین بکشید
      </div>
    </div>
  );
}

function ExploreTab() {
  const [items, setItems] = useState<ExploreItem[]>([]);
  const [feedbackMap, setFeedbackMap] = useState<Record<number, ExploreAnswerResult>>({});
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const fetchMore = () => {
    api
      .getExploreFeed()
      .then((res) => {
        setItems((prev) => {
          const existing = new Set(prev.map((i) => i.question_id));
          const fresh = res.items.filter((i) => !existing.has(i.question_id));
          return [...prev, ...fresh];
        });
      })
      .catch((e: Error) => setError(e.message))
      .finally(() => setLoading(false));
  };

  useEffect(() => {
    fetchMore();
  }, []);

  const handleAnswer = async (questionId: number, chosenIdx: number, activeMs: number) => {
    try {
      const res = await api.submitExploreAnswer(questionId, chosenIdx, activeMs);
      setFeedbackMap((prev) => ({ ...prev, [questionId]: res }));
    } catch (e) {
      setError((e as Error).message);
    }
  };

  const handleLeave = (questionId: number, activeMs: number) => {
    api.sendExploreEngagement(questionId, activeMs).catch(() => {});
    const idx = items.findIndex((i) => i.question_id === questionId);
    if (idx >= 0 && items.length - idx <= 3) {
      fetchMore();
    }
  };

  if (loading && items.length === 0) {
    return <div className="p-4 text-sm text-slate-400">در حال بارگذاری کاوش…</div>;
  }

  if (error && items.length === 0) {
    return (
      <div className="rounded-2xl border border-rose-800 bg-rose-950/40 p-4 text-xs text-rose-200">
        {error}
      </div>
    );
  }

  if (items.length === 0) {
    return (
      <div className="rounded-2xl border border-slate-800 bg-slate-900 p-6 text-center text-sm text-slate-400">
        در حال حاضر سؤال جدیدی در کاوش موجود نیست.
      </div>
    );
  }

  return (
    <div className="snap-y snap-mandatory space-y-4 overflow-y-auto h-[calc(100vh-6rem)]">
      {items.map((item) => (
        <ExploreCard
          key={item.question_id}
          item={item}
          feedback={feedbackMap[item.question_id]}
          onAnswer={handleAnswer}
          onLeave={handleLeave}
        />
      ))}
    </div>
  );
}

export default function App() {
  const [tab, setTab] = useState<Tab>("account");

  const tabs: { id: Tab; label: string }[] = [
    { id: "account", label: "حساب من" },
    { id: "library", label: "کتابخانه" },
    { id: "review", label: "مرور" },
    { id: "explore", label: "کاوش" },
  ];

  return (
    <div className="mx-auto flex min-h-screen max-w-md flex-col pb-20">
      <main className="flex-1 p-4">
        {tab === "account" && <MyAccountTab />}
        {tab === "library" && <LibraryTab />}
        {tab === "review" && <ReviewTab />}
        {tab === "explore" && <ExploreTab />}
      </main>
      <nav className="fixed bottom-0 left-0 right-0 mx-auto flex max-w-md border-t border-slate-800 bg-slate-950/95 backdrop-blur">
        {tabs.map((t) => (
          <button
            key={t.id}
            onClick={() => setTab(t.id)}
            className={`flex-1 py-3 text-xs font-medium ${
              tab === t.id ? "text-sky-400" : "text-slate-400"
            }`}
          >
            {t.label}
          </button>
        ))}
      </nav>
    </div>
  );
}
