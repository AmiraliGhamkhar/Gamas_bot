import { useCallback, useState } from "react";
import monogram from "./assets/brand/monogram.png";
import { api, MeResponse } from "./api";
import { AccountTab } from "./features/account/AccountTab";
import { ExploreTab } from "./features/explore/ExploreTab";
import { LibraryTab } from "./features/library/LibraryTab";
import { ReviewTab } from "./features/review/ReviewTab";
import { haptic, isInsideTelegram, isInsideTelegramClient } from "./lib/telegram";
import { useAsync } from "./lib/useAsync";
import { EcgLoader } from "./ui/Ecg";
import { Icon, IconName } from "./ui/Icon";
import { Button } from "./ui/primitives";
import { ErrorState } from "./ui/states";

type Tab = "review" | "library" | "explore" | "account";

/* Study first: the tab bar reads مرور · کتابخانه · یک‌سؤال · حساب من. */
const TABS: { key: Tab; label: string; icon: IconName }[] = [
  { key: "review", label: "مرور", icon: "review" },
  { key: "library", label: "کتابخانه", icon: "library" },
  { key: "explore", label: "یک‌سؤال", icon: "explore" },
  { key: "account", label: "حساب من", icon: "account" },
];

function Monogram({ size = 56 }: { size?: number }) {
  return (
    <img
      src={monogram}
      alt="نشان گاماس"
      width={size}
      height={size}
      className="rounded-[18px]"
      style={{ width: size, height: size }}
    />
  );
}

function BottomNav({
  active,
  onChange,
}: {
  active: Tab;
  onChange: (tab: Tab) => void;
}) {
  return (
    <nav
      className="fixed inset-x-0 bottom-0 z-20 border-t border-line bg-bg"
      style={{ paddingBottom: "calc(env(safe-area-inset-bottom, 0px) + 6px)" }}
    >
      <div
        role="tablist"
        aria-label="بخش‌های گاماس"
        className="mx-auto flex w-full max-w-md items-stretch justify-between gap-0.5 px-2 pt-2"
      >
        {TABS.map((tab) => {
          const isActive = tab.key === active;
          return (
            <button
              key={tab.key}
              type="button"
              role="tab"
              aria-selected={isActive}
              onClick={() => {
                if (isActive) return;
                haptic("select");
                onChange(tab.key);
              }}
              className="pressable flex min-h-[48px] flex-1 flex-col items-center justify-center gap-1 rounded-field px-1 py-1.5"
            >
              <span
                className={`flex h-7 w-12 items-center justify-center rounded-pill transition-colors ${
                  isActive ? "bg-brandSoft text-brand" : "text-faint"
                }`}
              >
                <Icon name={tab.icon} size={20} strokeWidth={isActive ? 2 : 1.7} />
              </span>
              <span
                className={`text-[10px] font-medium leading-none ${
                  isActive ? "text-brand" : "text-faint"
                }`}
              >
                {tab.label}
              </span>
            </button>
          );
        })}
      </div>
    </nav>
  );
}

function Splash() {
  return (
    <div className="flex min-h-[60vh] flex-col items-center justify-center gap-5" aria-busy="true">
      <Monogram />
      <EcgLoader label="در حال باز کردن گاماس" />
      <p className="text-xs text-faint">در حال باز کردن گاماس…</p>
    </div>
  );
}

export default function App() {
  const me = useAsync<MeResponse>(api.getMe, []);
  const [tab, setTab] = useState<Tab>("review");
  const [mounted, setMounted] = useState<Set<Tab>>(() => new Set<Tab>(["review"]));
  // A pack opened from the Library hands the Review tab its group to show.
  const [reviewPackId, setReviewPackId] = useState<number | null>(null);

  const changeTab = useCallback((next: Tab) => {
    setTab(next);
    setMounted((prev) => (prev.has(next) ? prev : new Set(prev).add(next)));
    window.scrollTo({ top: 0, behavior: "auto" });
  }, []);

  const openReviewForPack = useCallback(
    (packId: number) => {
      setReviewPackId(packId);
      changeTab("review");
    },
    [changeTab],
  );

  const reloadMe = useCallback(() => me.reload(), [me]);

  if (me.loading && !me.data) return <Splash />;

  if (!me.data) {
    // Outside Telegram there is no initData to authenticate with, so this is a
    // destination, not a failure: the app never asks for credentials itself.
    if (!isInsideTelegram()) {
      // Inside a Telegram client with no WebApp SDK means the script never
      // loaded (telegram.org unreachable and no self-hosted fallback): say
      // exactly that instead of telling an in-Telegram user to open Telegram.
      if (isInsideTelegramClient()) {
        return (
          <div className="mx-auto w-full max-w-md px-6 py-16 text-center">
            <span className="mx-auto flex">
              <Monogram />
            </span>
            <p className="mt-5 text-base font-semibold text-text">
              ابزار تلگرام بارگذاری نشد
            </p>
            <p className="mx-auto mt-2 max-w-[32ch] text-xs leading-6 text-faint">
              گاماس داخل تلگرام باز شده است اما اسکریپت وب‌اپ تلگرام در دسترس نبود.
              اینترنت خود را بررسی کنید و دوباره تلاش کنید.
            </p>
            <div className="mt-5 flex justify-center">
              <Button
                variant="secondary"
                icon="refresh"
                onClick={() => window.location.reload()}
              >
                تلاش دوباره
              </Button>
            </div>
          </div>
        );
      }
      return (
        <div className="mx-auto w-full max-w-md px-6 py-16 text-center">
          <span className="mx-auto flex">
            <Monogram />
          </span>
          <p className="mt-5 text-base font-semibold text-text">
            گاماس داخل تلگرام باز می‌شود
          </p>
          <p className="mx-auto mt-2 max-w-[30ch] text-xs leading-6 text-faint">
            این صفحه با حساب تلگرام شما کار می‌کند. ربات گاماس را باز کنید و دکمهٔ
            «گاماس» را بزنید.
          </p>
        </div>
      );
    }
    return (
      <div className="mx-auto w-full max-w-md space-y-4 px-4 py-10">
        <div className="text-center">
          <p className="text-sm font-medium text-text">گاماس باز نشد</p>
          <p className="mt-1 text-xs leading-6 text-faint">
            اتصال به سرور برقرار نشد.
          </p>
        </div>
        <ErrorState message={me.error ?? "خطای نامشخص"} onRetry={me.reload} />
      </div>
    );
  }

  const currentUser = me.data;

  if (currentUser.banned) {
    return (
      <div className="mx-auto w-full max-w-md px-4 py-12 text-center">
        <span className="mx-auto flex h-12 w-12 items-center justify-center rounded-full bg-badSoft text-bad">
          <Icon name="lock" size={22} />
        </span>
        <p className="mt-4 text-sm font-medium text-text">دسترسی حساب شما بسته است</p>
        <p className="mt-1 text-xs leading-6 text-faint">
          برای پیگیری از پشتیبانی ربات گاماس کمک بگیرید.
        </p>
      </div>
    );
  }

  return (
    <div className="relative mx-auto w-full max-w-md px-4 pb-28 pt-4">
      {/* On wide screens the navy void carries the monogram, quietly. */}
      <div
        aria-hidden
        className="pointer-events-none fixed inset-0 z-0 hidden items-center justify-center sm:flex"
      >
        <img
          src={monogram}
          alt=""
          className="h-[min(72vw,520px)] w-auto opacity-[0.035]"
        />
      </div>

      <div className="relative z-10">
        {mounted.has("review") && (
          <div role="tabpanel" aria-label="مرور" hidden={tab !== "review"}>
            <ReviewTab
              initialPackId={reviewPackId}
              onConsumedInitial={() => setReviewPackId(null)}
            />
          </div>
        )}

        {mounted.has("library") && (
          <div role="tabpanel" aria-label="کتابخانه" hidden={tab !== "library"}>
            <LibraryTab onOpenReview={openReviewForPack} />
          </div>
        )}

        {mounted.has("explore") && (
          <div role="tabpanel" aria-label="یک‌سؤال" hidden={tab !== "explore"}>
            <ExploreTab onReward={reloadMe} />
          </div>
        )}

        {mounted.has("account") && (
          <div role="tabpanel" aria-label="حساب من" hidden={tab !== "account"}>
            <AccountTab
              me={currentUser}
              onMeChanged={reloadMe}
              onGoReview={() => changeTab("review")}
              onGoLibrary={() => changeTab("library")}
            />
          </div>
        )}
      </div>

      <BottomNav active={tab} onChange={changeTab} />
    </div>
  );
}
