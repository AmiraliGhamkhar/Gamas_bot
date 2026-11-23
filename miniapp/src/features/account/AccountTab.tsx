import { useMemo, useState } from "react";
import { api, MeResponse, PlanOption } from "../../api";
import { formatDate, formatMoney, formatNumber, splitCredit, toFa } from "../../lib/format";
import { copyText, getTelegramUser, haptic, openTelegramLink, shareLink } from "../../lib/telegram";
import { useAsync } from "../../lib/useAsync";
import { Icon } from "../../ui/Icon";
import { Button, Card, Chip, Divider, SectionTitle, Stat } from "../../ui/primitives";
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
  return `${toFa(plan.hours)} ساعته`;
}

function validityLabel(days: number | null): string {
  if (days === null || days === undefined) return "بدون انقضا";
  return `اعتبار ${toFa(days)} روزه`;
}

export function AccountTab({
  me,
  onMeChanged,
}: {
  me: MeResponse;
  onMeChanged: () => void;
}) {
  const plan = useAsync(api.getPlan, []);
  const plans = useAsync(api.getPlans, []);
  const refs = useAsync(api.getReferrals, []);
  const pays = useAsync(api.getPayments, []);
  const catalog = useAsync(api.getRewardCatalog, []);
  const sub = useAsync(api.getSubscription, []);

  const [selectedPlan, setSelectedPlan] = useState<string | null>(null);
  const [copied, setCopied] = useState<"id" | "invite" | null>(null);
  const [redeemState, setRedeemState] = useState<{
    code: string;
    message: string;
    ok: boolean;
  } | null>(null);

  const tgUser = getTelegramUser();
  const displayName =
    [tgUser.first_name, tgUser.last_name].filter(Boolean).join(" ") || "کاربر گاماس";
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
    haptic("tap");
    const key =
      typeof crypto !== "undefined" && "randomUUID" in crypto
        ? crypto.randomUUID()
        : `${Date.now()}-${Math.random().toString(36).slice(2)}`;
    try {
      const res = await api.redeemReward(code, key);
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
      haptic("error");
      setRedeemState({
        code,
        ok: false,
        message: (error as Error).message || "تبدیل امتیاز انجام نشد.",
      });
    }
  };

  return (
    <div className="space-y-5">
      {/* Identity */}
      <Card className="flex items-center gap-3.5">
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
      </Card>

      {/* Balance */}
      <Card className="space-y-4">
        <div className="flex items-start justify-between gap-3">
          <div>
            <p className="text-xs text-faint">اعتبار گاماس</p>
            <p className="mt-1.5 flex items-baseline gap-1.5">
              <span className="nums text-[30px] font-semibold leading-none text-text">
                {unlimited ? "نامحدود" : credit.value}
              </span>
              {!unlimited && (
                <span className="text-xs text-muted">
                  {credit.unit}
                  {credit.rest ? ` و ${credit.rest}` : ""}
                </span>
              )}
            </p>
          </div>
          <span className="flex h-9 w-9 items-center justify-center rounded-full bg-brandSoft text-brand">
            <Icon name="clock" size={18} />
          </span>
        </div>
        <Divider />
        <div className="grid grid-cols-2 gap-3">
          <Stat
            label="امتیاز پاداش"
            icon="spark"
            value={formatNumber(me.reward_points)}
            hint="جدا از اعتبار"
          />
          <Stat
            label="اعتبار فعال تا"
            icon="trend"
            value={
              unlimited
                ? "بدون محدودیت"
                : plan.data?.expires_at
                  ? formatDate(plan.data.expires_at)
                  : "بدون انقضا"
            }
            hint={currentPlan ? `پلن ${toFa(currentPlan.hours ?? 0)} ساعته` : undefined}
          />
        </div>
      </Card>

      {/* Subscription + purchase options */}
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
            icon="lock"
            title="فروش اشتراک در حال حاضر فعال نیست"
            description="به‌زودی بسته‌های اعتبار در همین بخش نمایش داده می‌شوند."
          />
        )}
        {options.length > 0 && (
          <Card className="space-y-3 p-3">
            <div className="space-y-1.5" role="radiogroup" aria-label="بسته‌های اعتبار">
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
                    className={`pressable flex w-full items-center gap-3 rounded-field border px-3 py-3 text-right transition-colors ${
                      active
                        ? "border-brand/60 bg-brandSoft"
                        : "border-line bg-surface2 hover:bg-surface3"
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
                پلن فعلی شما {toFa(currentPlan.hours ?? 0)} ساعته است؛ خرید جدید به اعتبار
                فعلی اضافه می‌شود.
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
          </Card>
        )}
      </section>

      {/* Referrals */}
      <section>
        <SectionTitle title="دعوت دوستان" hint="با هر دعوت موفق امتیاز هدیه بگیرید." />
        {refs.error && !refs.data ? (
          <ErrorState compact message={refs.error} onRetry={refs.reload} />
        ) : (
          <Card className="space-y-3">
            <div className="flex items-center justify-between">
              <span className="flex items-center gap-2 text-sm text-text">
                <Icon name="account" size={17} className="text-muted" />
                دعوت موفق
              </span>
              <span className="nums text-lg font-semibold text-brand">
                {refs.loading ? "…" : formatNumber(refs.data?.invited_count ?? 0)}
              </span>
            </div>
            <Divider />
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
          </Card>
        )}
      </section>

      {/* Reward balance + redemption */}
      <section>
        <SectionTitle title="امتیاز پاداش" hint="امتیاز از پاسخ درست در اکسپلور به دست می‌آید." />
        <Card className="space-y-3">
          <div className="flex items-center justify-between">
            <span className="text-sm text-muted">موجودی</span>
            <span className="nums text-xl font-semibold text-brand">
              {formatNumber(me.reward_points)}
            </span>
          </div>
          {catalog.error && !catalog.data && (
            <ErrorState compact message={catalog.error} onRetry={catalog.reload} />
          )}
          {catalog.loading && !catalog.data && <div className="skeleton h-10 w-full" />}
          {!catalog.loading && !catalog.error && (catalog.data?.catalog.length ?? 0) === 0 && (
            <InlineNote>در حال حاضر جایزه‌ای برای تبدیل امتیاز تعریف نشده است.</InlineNote>
          )}
          {(catalog.data?.catalog ?? []).map((item) => {
            const affordable = me.reward_points >= item.cost_points;
            return (
              <div key={item.code} className="space-y-1.5">
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
                    disabled={!affordable}
                    onClick={() => redeem(item.code)}
                    className="px-3 text-xs"
                  >
                    تبدیل
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
        </Card>
      </section>

      {/* Payment history */}
      <section>
        <SectionTitle title="تاریخچهٔ پرداخت" />
        {pays.error && !pays.data ? (
          <ErrorState compact message={pays.error} onRetry={pays.reload} />
        ) : pays.loading && !pays.data ? (
          <ListSkeleton rows={2} />
        ) : (pays.data?.payments.length ?? 0) === 0 ? (
          <EmptyState
            icon="doc"
            title="پرداختی ثبت نشده است"
            description="پس از خرید اشتراک، رسید و وضعیت آن در همین بخش دیده می‌شود."
          />
        ) : (
          <Card className="divide-y divide-line2 p-0">
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
          </Card>
        )}
      </section>

      {/* Support + channel */}
      <section className="space-y-2">
        <Button
          block
          icon="send"
          onClick={() => openTelegramLink(`https://t.me/${botUsername}?start=support`)}
        >
          پشتیبانی در ربات
        </Button>
        {sub.data?.channel_url && sub.data.required_channel && (
          <Button
            block
            variant="ghost"
            icon="lock"
            onClick={() => openTelegramLink(sub.data!.channel_url!)}
          >
            عضویت در کانال {sub.data.required_channel}
          </Button>
        )}
        <p className="pt-1 text-center text-[10px] leading-5 text-faint">
          اعتبار، پرداخت و امتیاز از سرور گاماس خوانده می‌شود.
        </p>
      </section>
    </div>
  );
}
