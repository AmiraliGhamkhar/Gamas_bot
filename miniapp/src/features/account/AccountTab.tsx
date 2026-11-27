import { useMemo, useRef, useState } from "react";
import { api, MeResponse, PlanOption } from "../../api";
import { formatDate, formatMoney, formatNumber, splitCredit, toFa } from "../../lib/format";
import { copyText, getTelegramUser, haptic, openTelegramLink, shareLink } from "../../lib/telegram";
import { describeError, useAsync } from "../../lib/useAsync";
import { Icon } from "../../ui/Icon";
import {
  Banner,
  Button,
  Chip,
  Divider,
  Group,
  Row,
  SectionTitle,
  Sheet,
} from "../../ui/primitives";
import { EmptyState, ErrorState, InlineNote, ListSkeleton } from "../../ui/states";

const PAYMENT_STATUS: Record<string, { label: string; tone: "neutral" | "brand" | "ok" | "bad" }> = {
  approved: { label: "تأییدشده", tone: "ok" },
  pending: { label: "در انتظار پرداخت", tone: "neutral" },
  awaiting_payment: { label: "در انتظار پرداخت", tone: "neutral" },
  pending_review: { label: "در انتظار بررسی", tone: "brand" },
  rejected: { label: "ردشده", tone: "bad" },
  canceled: { label: "لغوشده", tone: "neutral" },
  cancelled: { label: "لغوشده", tone: "neutral" },
};

function planLabel(plan: PlanOption): string {
  return `بستهٔ ${toFa(plan.hours)} ساعته`;
}

function validityLabel(days: number | null): string {
  if (days === null || days === undefined) return "بدون انقضا";
  return `اعتبار ${toFa(days)} روزه`;
}

export function AccountTab({
  me,
  onMeChanged,
  onGoReview,
  onGoLibrary,
}: {
  me: MeResponse;
  onMeChanged: () => void;
  onGoReview: () => void;
  onGoLibrary: () => void;
}) {
  const plan = useAsync(api.getPlan, []);
  const plans = useAsync(api.getPlans, []);
  const refs = useAsync(api.getReferrals, []);
  const pays = useAsync(api.getPayments, []);
  const catalog = useAsync(api.getRewardCatalog, []);
  const sub = useAsync(api.getSubscription, []);

  const [selectedPlan, setSelectedPlan] = useState<string | null>(null);
  const [copied, setCopied] = useState<"id" | "invite" | null>(null);
  const [walletOpen, setWalletOpen] = useState(false);
  const [redeemState, setRedeemState] = useState<{
    code: string;
    message: string;
    ok: boolean;
  } | null>(null);
  // One in-flight redemption at a time, and one idempotency key per attempt:
  // a retry (double tap, flaky network) re-sends the same key so the server
  // replays the original result instead of spending the points twice.
  const [redeemingCode, setRedeemingCode] = useState<string | null>(null);
  const redeemKeys = useRef(new Map<string, string>());

  const tgUser = getTelegramUser();
  const displayName =
    [tgUser.first_name, tgUser.last_name].filter(Boolean).join(" ") || "دانش‌آموز گاماس";
  const initial = displayName.trim().slice(0, 1) || "گ";
  const botUsername = me.bot_username || "Gamas_bot";
  const inviteLink = `https://t.me/${botUsername}?start=ref_${me.user_id}`;

  const options = plans.data?.plans ?? [];
  const chosenPlan: PlanOption | null = useMemo(() => {
    if (!options.length) return null;
    return options.find((p) => p.code === selectedPlan) ?? options[0];
  }, [options, selectedPlan]);

  const currentPlan = plan.data?.plan ?? null;
  const unlimited = Boolean(plan.data?.unlimited);
  const credit = splitCredit(me.credit_seconds);

  const copy = async (value: string, which: "id" | "invite") => {
    const done = await copyText(value);
    haptic(done ? "success" : "error");
    if (done) {
      setCopied(which);
      setTimeout(() => setCopied(null), 1800);
    }
  };

  const redeem = async (code: string) => {
    if (redeemingCode !== null) return;
    haptic("tap");
    setRedeemingCode(code);
    setRedeemState(null);
    let key = redeemKeys.current.get(code);
    if (!key) {
      key =
        typeof crypto !== "undefined" && "randomUUID" in crypto
          ? crypto.randomUUID()
          : `${Date.now()}-${Math.random().toString(36).slice(2)}`;
      redeemKeys.current.set(code, key);
    }
    try {
      const res = await api.redeemReward(code, key);
      // The key produced a committed redemption; the next one is a new attempt.
      redeemKeys.current.delete(code);
      const minutes = Math.floor(res.credit_seconds_granted / 60);
      setRedeemState({
        code,
        ok: true,
        message:
          minutes > 0
            ? `${toFa(minutes)} دقیقه اعتبار به حساب شما اضافه شد.`
            : "جایزه در حساب شما فعال شد.",
      });
      haptic("success");
      onMeChanged();
      plan.reload();
    } catch (error) {
      // Keep the key: nothing was spent on the server (a failed redemption
      // never consumes it), so a retry must stay idempotent.
      haptic("error");
      setRedeemState({
        code,
        ok: false,
        message: describeError(error) || "تبدیل امتیاز انجام نشد.",
      });
    } finally {
      setRedeemingCode(null);
    }
  };

  return (
    <div className="space-y-5">
      {/* Identity — one flat row, hairline below. */}
      <div className="flex items-center gap-3.5 border-b border-line2 pb-4">
        {tgUser.photo_url ? (
          <img
            src={tgUser.photo_url}
            alt=""
            className="h-14 w-14 shrink-0 rounded-full object-cover"
          />
        ) : (
          <span className="flex h-14 w-14 shrink-0 items-center justify-center rounded-full bg-brandSoft text-lg font-semibold text-brand">
            {initial}
          </span>
        )}
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2">
            <p className="truncate text-[15px] font-semibold text-text">{displayName}</p>
            {unlimited && <Chip tone="brand">ویژه</Chip>}
          </div>
          {tgUser.username && (
            <p className="truncate text-xs text-muted" dir="ltr">
              @{tgUser.username}
            </p>
          )}
          <button
            type="button"
            onClick={() => copy(String(me.user_id), "id")}
            className="pressable mt-1 inline-flex items-center gap-1 text-[11px] text-faint"
            aria-label="کپی شناسهٔ کاربری"
          >
            <Icon name="copy" size={13} />
            <span className="nums" dir="ltr">
              {toFa(me.user_id)}
            </span>
            <span className="text-faint">{copied === "id" ? "کپی شد" : "شناسه"}</span>
          </button>
        </div>
      </div>

      {/* Balance — the screen's one navy banner. */}
      <Banner>
        <div className="flex items-start justify-between gap-3">
          <div>
            <p className="text-xs text-paper/70">اعتبار گاماس</p>
            <p className="mt-1.5 flex items-baseline gap-1.5">
              <span className="nums text-[30px] font-semibold leading-none text-paper">
                {unlimited ? "نامحدود" : credit.value}
              </span>
              {!unlimited && (
                <span className="text-xs text-paper/70">
                  {credit.unit}
                  {credit.rest ? ` و ${credit.rest}` : ""}
                </span>
              )}
            </p>
          </div>
          <button
            type="button"
            onClick={() => {
              haptic("tap");
              setWalletOpen(true);
            }}
            className="pressable -mt-1 inline-flex items-center gap-1.5 rounded-pill border border-brand/40 px-3 py-1.5 text-xs font-medium text-brand hover:bg-brandSoft"
          >
            <Icon name="wallet" size={15} />
            خرید اشتراک
          </button>
        </div>
        <div className="mt-3 flex flex-wrap items-center gap-x-2.5 gap-y-1 pb-1.5 text-[11px] text-paper/70">
          <span>
            امتیاز پاداش: <span className="nums">{formatNumber(me.reward_points)}</span>
          </span>
          <span aria-hidden>·</span>
          <span>
            {unlimited
              ? "اعتبار نامحدود"
              : plan.data?.expires_at
                ? `فعال تا ${formatDate(plan.data.expires_at)}`
                : "بدون انقضا"}
          </span>
          {currentPlan && !unlimited && (
            <>
              <span aria-hidden>·</span>
              <span>
                بستهٔ <span className="nums">{toFa(currentPlan.hours ?? 0)}</span> ساعته
              </span>
            </>
          )}
        </div>
      </Banner>

      {/* Study first: what to do here, before anything about money. */}
      <Group title="ادامهٔ مطالعه">
        <Row
          icon="review"
          title="ادامهٔ مرور"
          meta="سؤال‌های ساخته‌شده از فایل‌های شما"
          onClick={() => {
            haptic("tap");
            onGoReview();
          }}
        />
        <Row
          icon="library"
          title="بسته‌های من"
          meta="جزوه‌ها و خروجی‌ها"
          onClick={() => {
            haptic("tap");
            onGoLibrary();
          }}
        />
      </Group>

      {/* Wallet and support — money lives in the sheet, not on the tab. */}
      <Group title="گاماس">
        <Row
          icon="wallet"
          title="کیف پول"
          meta="اشتراک، امتیاز، پرداخت‌ها و دعوت"
          onClick={() => {
            haptic("tap");
            setWalletOpen(true);
          }}
        />
        <Row
          icon="send"
          title="پشتیبانی در ربات"
          meta="پیگیری خرید و مشکلات"
          onClick={() => {
            haptic("tap");
            openTelegramLink(`https://t.me/${botUsername}?start=support`);
          }}
        />
        {sub.data?.channel_url && sub.data.required_channel && (
          <Row
            icon="lock"
            title={`عضویت در کانال ${sub.data.required_channel}`}
            onClick={() => {
              haptic("tap");
              openTelegramLink(sub.data!.channel_url!);
            }}
          />
        )}
      </Group>

      <p className="pt-1 text-center text-[10px] leading-5 text-faint">
        اعتبار، پرداخت و امتیاز از سرور گاماس خوانده می‌شود.
      </p>

      <Sheet open={walletOpen} onClose={() => setWalletOpen(false)} title="کیف پول">
        {/* Subscription */}
        <section>
          <SectionTitle
            title="اشتراک"
            hint="خرید و تأیید رسید در ربات گاماس انجام می‌شود."
          />
          {plans.loading && !plans.data && <ListSkeleton rows={1} />}
          {plans.error && !plans.data && (
            <ErrorState compact message={plans.error} onRetry={plans.reload} />
          )}
          {!plans.loading && !plans.error && options.length === 0 && (
            <EmptyState
              emblem="capsule"
              title="فروش اشتراک در حال حاضر فعال نیست"
              description="به‌زودی بسته‌های اعتبار در همین بخش نمایش داده می‌شوند."
            />
          )}
          {options.length > 0 && (
            <div className="space-y-3">
              <div
                className="divide-y divide-line2 overflow-hidden rounded-card border border-line bg-surface2"
                role="radiogroup"
                aria-label="بسته‌های اعتبار"
              >
                {options.map((option) => {
                  const active = chosenPlan?.code === option.code;
                  return (
                    <button
                      key={option.code}
                      type="button"
                      role="radio"
                      aria-checked={active}
                      onClick={() => {
                        haptic("select");
                        setSelectedPlan(option.code);
                      }}
                      className={`pressable flex w-full items-center gap-3 px-3 py-3 text-right transition-colors ${
                        active ? "bg-brandSoft" : "hover:bg-surface3"
                      }`}
                    >
                      <span
                        className={`flex h-5 w-5 shrink-0 items-center justify-center rounded-full border ${
                          active ? "border-brand bg-brand text-brandInk" : "border-line"
                        }`}
                        aria-hidden
                      >
                        {active && <Icon name="check" size={12} strokeWidth={3} />}
                      </span>
                      <span className="min-w-0 flex-1">
                        <span className="flex items-baseline justify-between gap-2">
                          <span className="text-sm font-medium text-text">
                            {planLabel(option)}
                          </span>
                          <span className="nums text-sm font-semibold text-text">
                            {formatMoney(option.price)}
                          </span>
                        </span>
                        <span className="mt-0.5 flex items-center justify-between gap-2 text-[11px] text-faint">
                          <span>{validityLabel(option.validity_days)}</span>
                          {option.original_price && option.original_price > option.price && (
                            <span className="nums line-through opacity-70">
                              {formatMoney(option.original_price)}
                            </span>
                          )}
                        </span>
                      </span>
                    </button>
                  );
                })}
              </div>
              {currentPlan && (
                <p className="px-1 text-[11px] leading-5 text-faint">
                  بستهٔ فعلی شما {toFa(currentPlan.hours ?? 0)} ساعته است؛ خرید جدید به
                  اعتبار فعلی اضافه می‌شود.
                </p>
              )}
              <Button
                variant="primary"
                block
                icon="send"
                disabled={!chosenPlan}
                onClick={() => {
                  haptic("tap");
                  openTelegramLink(`https://t.me/${botUsername}?start=buy`);
                }}
              >
                خرید در ربات
              </Button>
            </div>
          )}
        </section>

        <Divider />

        {/* Reward balance + redemption */}
        <section>
          <SectionTitle
            title="امتیاز پاداش"
            hint="امتیاز از پاسخ درست در یک‌سؤال به دست می‌آید."
          />
          <div className="divide-y divide-line2 overflow-hidden rounded-card border border-line bg-surface2">
            <div className="flex items-center justify-between px-4 py-3">
              <span className="text-sm text-muted">موجودی امتیاز</span>
              <span className="nums text-xl font-semibold text-brand">
                {formatNumber(me.reward_points)}
              </span>
            </div>
            {catalog.loading && !catalog.data && (
              <div className="px-4 py-3">
                <div className="skeleton h-10 w-full" />
              </div>
            )}
            {(catalog.data?.catalog ?? []).map((item) => {
              const affordable = me.reward_points >= item.cost_points;
              return (
                <div key={item.code} className="space-y-1.5 px-4 py-3">
                  <div className="flex items-center justify-between gap-2">
                    <div className="min-w-0">
                      <p className="truncate text-sm text-text">{item.title}</p>
                      <p className="nums text-[11px] text-faint">
                        {formatNumber(item.cost_points)} امتیاز
                        {!affordable && (
                          <span className="mr-1 text-faint">
                            · {formatNumber(item.cost_points - me.reward_points)} امتیاز کم
                            دارید
                          </span>
                        )}
                      </p>
                    </div>
                    <Button
                      disabled={!affordable || redeemingCode !== null}
                      onClick={() => redeem(item.code)}
                      className="px-3 text-xs"
                    >
                      {redeemingCode === item.code ? "در حال تبدیل…" : "تبدیل"}
                    </Button>
                  </div>
                  {redeemState?.code === item.code && (
                    <p
                      className={`text-[11px] leading-5 ${
                        redeemState.ok ? "text-ok" : "text-bad"
                      }`}
                      role="status"
                    >
                      {redeemState.message}
                    </p>
                  )}
                </div>
              );
            })}
          </div>
          {catalog.error && !catalog.data && (
            <ErrorState compact message={catalog.error} onRetry={catalog.reload} />
          )}
          {!catalog.loading && !catalog.error && (catalog.data?.catalog.length ?? 0) === 0 && (
            <InlineNote>در حال حاضر جایزه‌ای برای تبدیل امتیاز تعریف نشده است.</InlineNote>
          )}
        </section>

        <Divider />

        {/* Payment history */}
        <section>
          <SectionTitle title="تاریخچهٔ پرداخت" />
          {pays.error && !pays.data ? (
            <ErrorState compact message={pays.error} onRetry={pays.reload} />
          ) : pays.loading && !pays.data ? (
            <ListSkeleton rows={2} />
          ) : (pays.data?.payments.length ?? 0) === 0 ? (
            <EmptyState
              emblem="chart"
              title="پرداختی ثبت نشده است"
              description="پس از خرید اشتراک، رسید و وضعیت آن در همین بخش دیده می‌شود."
            />
          ) : (
            <div className="divide-y divide-line2 overflow-hidden rounded-card border border-line bg-surface2">
              {(pays.data?.payments ?? []).map((payment) => {
                const status =
                  PAYMENT_STATUS[payment.status] ?? {
                    label: payment.status,
                    tone: "neutral" as const,
                  };
                return (
                  <div
                    key={payment.payment_id}
                    className="flex items-center justify-between gap-3 px-4 py-3"
                  >
                    <div className="min-w-0">
                      <p className="nums text-sm font-medium text-text">
                        {formatMoney(payment.amount_toman)}
                      </p>
                      <p className="mt-0.5 text-[11px] text-faint">
                        {formatDate(payment.created_at)}
                      </p>
                    </div>
                    <Chip tone={status.tone} dot>
                      {status.label}
                    </Chip>
                  </div>
                );
              })}
            </div>
          )}
        </section>

        <Divider />

        {/* Referrals */}
        <section>
          <SectionTitle title="دعوت دوستان" hint="با هر دعوت موفق امتیاز هدیه بگیرید." />
          {refs.error && !refs.data ? (
            <ErrorState compact message={refs.error} onRetry={refs.reload} />
          ) : (
            <div className="space-y-3">
              <div className="flex items-center justify-between rounded-card border border-line bg-surface2 px-4 py-3">
                <span className="flex items-center gap-2 text-sm text-text">
                  <Icon name="account" size={17} className="text-muted" />
                  دعوت موفق
                </span>
                <span className="nums text-lg font-semibold text-brand">
                  {refs.loading ? "…" : formatNumber(refs.data?.invited_count ?? 0)}
                </span>
              </div>
              <div className="space-y-2">
                <p
                  className="truncate rounded-field bg-surface2 px-3 py-2 text-[11px] text-faint"
                  dir="ltr"
                >
                  {inviteLink}
                </p>
                <div className="flex gap-2">
                  <Button
                    block
                    icon="copy"
                    onClick={() => copy(inviteLink, "invite")}
                    aria-label="کپی لینک دعوت"
                  >
                    {copied === "invite" ? "کپی شد" : "کپی لینک"}
                  </Button>
                  <Button
                    block
                    variant="primary"
                    icon="share"
                    onClick={() => {
                      haptic("tap");
                      shareLink(inviteLink, "به گاماس بپیوند؛ جزوه و مرور هوشمند درس‌ها");
                    }}
                  >
                    اشتراک‌گذاری
                  </Button>
                </div>
              </div>
            </div>
          )}
        </section>
      </Sheet>
    </div>
  );
}
