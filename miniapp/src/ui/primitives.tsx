import {
  ButtonHTMLAttributes,
  ReactNode,
  useEffect,
  useRef,
} from "react";
import { toFa } from "../lib/format";
import { EcgRule } from "./Ecg";
import { Icon, IconName } from "./Icon";

/**
 * The app's surface vocabulary (see `miniapp/DESIGN.md`):
 *
 *   chrome  — Card / Group rows: flat navy, hairline borders
 *   paper   — PaperCard: warm cream, ink text, an ECG rule along the top
 *   banner  — Banner: the booklet's navy banner register, one per screen
 *
 * Anything that looks like a setting or a record is a Row inside a Group, not
 * a card; a card is reserved for things with real boundaries.
 */

export function Card({
  children,
  className = "",
  as: Tag = "div",
}: {
  children: ReactNode;
  className?: string;
  as?: "div" | "section" | "article";
}) {
  return <Tag className={`rounded-card border border-line bg-surface p-4 ${className}`}>{children}</Tag>;
}

export function PaperCard({
  children,
  tone = "brand",
  className = "space-y-4",
}: {
  children: ReactNode;
  /** The ECG rule carries the verdict: brand while unanswered, ok/bad after. */
  tone?: "brand" | "ok" | "bad";
  className?: string;
}) {
  const ruleTone =
    tone === "ok" ? "text-okInk" : tone === "bad" ? "text-badInk" : "text-trace";
  return (
    <div className="paper relative overflow-hidden">
      <EcgRule className={`absolute inset-x-0 top-0 h-3 w-full ${ruleTone}`} />
      <div className={`px-4 pb-4 pt-6 ${className}`}>{children}</div>
    </div>
  );
}

export function Banner({
  children,
  className = "",
}: {
  children: ReactNode;
  className?: string;
}) {
  return (
    <div
      className={`relative overflow-hidden rounded-card border border-line bg-banner ${className}`}
    >
      <div className="p-4">{children}</div>
      <EcgRule className="absolute inset-x-0 bottom-0 h-3 w-full text-brand/70" />
    </div>
  );
}

/** A grouped list: section label plus flat rows on hairline dividers. */
export function Group({
  title,
  children,
  className = "",
  listClassName = "",
}: {
  title?: string;
  children: ReactNode;
  className?: string;
  /** Extra classes for the row container (e.g. "stagger" on a fresh list). */
  listClassName?: string;
}) {
  return (
    <section className={className}>
      {title && (
        <h2 className="mb-2 px-1 text-[13px] font-medium text-muted">{title}</h2>
      )}
      <div
        className={`divide-y divide-line2 overflow-hidden rounded-card border border-line bg-surface ${listClassName}`}
      >
        {children}
      </div>
    </section>
  );
}

/** One flat row of a Group: icon, title, meta, trailing control. */
export function Row({
  icon,
  title,
  meta,
  trailing,
  onClick,
  className = "",
}: {
  icon?: IconName;
  title: ReactNode;
  meta?: ReactNode;
  trailing?: ReactNode;
  onClick?: () => void;
  className?: string;
}) {
  const body = (
    <>
      {icon && <Icon name={icon} size={19} className="shrink-0 text-faint" />}
      <span className="min-w-0 flex-1">
        <span className="block truncate text-sm font-medium text-text">{title}</span>
        {meta && <span className="mt-0.5 block truncate text-[11px] text-faint">{meta}</span>}
      </span>
      {trailing ?? (onClick ? <Icon name="forward" size={16} className="shrink-0 text-faint" /> : null)}
    </>
  );
  const classes = `flex w-full items-center gap-3 px-4 py-3.5 text-right ${className}`;
  return onClick ? (
    <button type="button" onClick={onClick} className={`pressable ${classes}`}>
      {body}
    </button>
  ) : (
    <div className={classes}>{body}</div>
  );
}

/**
 * A bottom sheet over the current tab (chrome register). Money and settings
 * live here so the tabs can stay about studying.
 */
export function Sheet({
  open,
  onClose,
  title,
  children,
}: {
  open: boolean;
  onClose: () => void;
  title: string;
  children: ReactNode;
}) {
  const panelRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKeyDown);
    return () => {
      document.body.style.overflow = previousOverflow;
      window.removeEventListener("keydown", onKeyDown);
    };
  }, [open, onClose]);

  if (!open) return null;
  return (
    <div
      role="dialog"
      aria-modal="true"
      aria-label={title}
      className="fixed inset-0 z-40 flex items-end justify-center"
    >
      <button
        type="button"
        aria-label="بستن"
        onClick={onClose}
        className="absolute inset-0 animate-fade bg-black/60"
      />
      <div
        ref={panelRef}
        className="safe-bottom relative max-h-[85vh] w-full max-w-md animate-sheet overflow-y-auto no-scrollbar rounded-t-[24px] border-t border-line bg-surface pb-4"
      >
        <div className="sticky top-0 z-10 flex items-center gap-3 border-b border-line2 bg-surface px-4 pb-3 pt-2.5">
          <span
            aria-hidden
            className="absolute inset-x-0 top-1 mx-auto h-1 w-10 rounded-pill bg-line"
          />
          <h2 className="flex-1 pt-1 text-[15px] font-semibold text-text">{title}</h2>
          <button
            type="button"
            onClick={onClose}
            aria-label="بستن"
            className="pressable flex h-9 w-9 items-center justify-center rounded-full text-muted hover:bg-surface2"
          >
            <Icon name="close" size={18} />
          </button>
        </div>
        <div className="space-y-5 p-4">{children}</div>
      </div>
    </div>
  );
}

export function SectionTitle({
  title,
  hint,
  action,
}: {
  title: string;
  hint?: string;
  action?: ReactNode;
}) {
  return (
    <div className="mb-2 flex items-baseline justify-between gap-3 px-1">
      <div className="min-w-0">
        <h2 className="text-[15px] font-semibold text-text">{title}</h2>
        {hint && <p className="mt-0.5 text-xs leading-5 text-faint">{hint}</p>}
      </div>
      {action}
    </div>
  );
}

type Tone = "neutral" | "brand" | "ok" | "bad";

const TONES: Record<Tone, string> = {
  neutral: "border-line bg-surface2 text-muted",
  brand: "border-brand/35 bg-brandSoft text-brand",
  ok: "border-ok/30 bg-okSoft text-ok",
  bad: "border-bad/30 bg-badSoft text-bad",
};

const PAPER_TONES: Record<Tone, string> = {
  neutral: "border-ink/15 bg-paper2 text-inkMuted",
  brand: "border-trace/30 bg-trace/12 text-trace",
  ok: "border-okInk/30 bg-ok/15 text-okInk",
  bad: "border-badInk/30 bg-bad/15 text-badInk",
};

export function Chip({
  children,
  tone = "neutral",
  dot = false,
  onPaper = false,
  className = "",
}: {
  children: ReactNode;
  tone?: Tone;
  dot?: boolean;
  /** Paper register: ink-based tones instead of the dark chrome ones. */
  onPaper?: boolean;
  className?: string;
}) {
  return (
    <span
      className={`inline-flex items-center gap-1.5 rounded-pill border px-2.5 py-1 text-[11px] font-medium ${
        onPaper ? PAPER_TONES[tone] : TONES[tone]
      } ${className}`}
    >
      {dot && <span className="h-1.5 w-1.5 rounded-full bg-current" aria-hidden />}
      {children}
    </span>
  );
}

type Variant = "primary" | "secondary" | "ghost" | "danger";

const VARIANTS: Record<Variant, string> = {
  primary:
    "bg-brand text-brandInk shadow-brand hover:brightness-[1.04] disabled:bg-surface3 disabled:text-faint disabled:shadow-none",
  secondary:
    "bg-surface3 text-text hover:bg-surface2 disabled:text-faint",
  ghost: "bg-transparent text-brand hover:bg-brandSoft disabled:text-faint",
  danger: "bg-badSoft text-bad hover:brightness-110 disabled:text-faint",
};

export function Button({
  children,
  variant = "secondary",
  icon,
  block = false,
  className = "",
  ...rest
}: ButtonHTMLAttributes<HTMLButtonElement> & {
  variant?: Variant;
  icon?: IconName;
  block?: boolean;
}) {
  return (
    <button
      type="button"
      {...rest}
      className={`pressable inline-flex min-h-[44px] items-center justify-center gap-2 rounded-field px-4 text-sm font-medium transition-colors disabled:cursor-not-allowed ${
        VARIANTS[variant]
      } ${block ? "w-full" : ""} ${className}`}
    >
      {icon && <Icon name={icon} size={18} />}
      {children}
    </button>
  );
}

export function ScreenHeader({
  title,
  subtitle,
  onBack,
  actions,
}: {
  title: string;
  subtitle?: ReactNode;
  onBack?: () => void;
  actions?: ReactNode;
}) {
  return (
    <header className="mb-4 flex items-start gap-3">
      {onBack && (
        <button
          type="button"
          onClick={onBack}
          aria-label="بازگشت"
          className="pressable -mr-1 mt-0.5 flex h-10 w-10 shrink-0 items-center justify-center rounded-full text-muted hover:bg-surface2"
        >
          {/* Back points to the right in a right-to-left layout. */}
          <Icon name="back" size={20} />
        </button>
      )}
      <div className="min-w-0 flex-1">
        <h1 className="truncate text-[19px] font-semibold leading-7 text-text">
          {title}
        </h1>
        {subtitle && (
          <div className="mt-0.5 text-xs leading-5 text-faint">{subtitle}</div>
        )}
      </div>
      {actions}
    </header>
  );
}

export function Progress({
  value,
  total,
  tone = "brand",
  label,
}: {
  value: number;
  total: number;
  tone?: "brand" | "ok";
  label?: string;
}) {
  const ratio = total > 0 ? Math.min(1, Math.max(0, value / total)) : 0;
  return (
    <div
      className="h-1.5 w-full overflow-hidden rounded-pill bg-surface3"
      role="progressbar"
      aria-valuemin={0}
      aria-valuemax={total}
      aria-valuenow={value}
      aria-label={label}
    >
      <div
        className={`h-full rounded-pill transition-[width] duration-500 ease-smooth ${
          tone === "ok" ? "bg-ok" : "bg-brand"
        }`}
        style={{ width: `${Math.round(ratio * 100)}%` }}
      />
    </div>
  );
}

export function Stat({
  label,
  value,
  hint,
  icon,
}: {
  label: string;
  value: ReactNode;
  hint?: string;
  icon?: IconName;
}) {
  return (
    <div className="min-w-0">
      <div className="flex items-center gap-1.5 text-[11px] text-faint">
        {icon && <Icon name={icon} size={14} />}
        <span>{label}</span>
      </div>
      <div className="nums mt-1 truncate text-[15px] font-semibold text-text">
        {value}
      </div>
      {hint && <div className="mt-0.5 truncate text-[11px] text-faint">{hint}</div>}
    </div>
  );
}

export function Segmented<T extends string>({
  options,
  value,
  onChange,
  ariaLabel,
}: {
  options: { value: T; label: string }[];
  value: T;
  onChange: (next: T) => void;
  ariaLabel: string;
}) {
  return (
    <div
      role="group"
      aria-label={ariaLabel}
      className="inline-flex items-center gap-1 rounded-pill border border-line bg-surface p-1"
    >
      {options.map((option) => {
        const active = option.value === value;
        return (
          <button
            key={option.value}
            type="button"
            onClick={() => onChange(option.value)}
            aria-pressed={active}
            className={`min-h-[36px] rounded-pill px-3.5 text-[11px] font-medium transition-colors ${
              active ? "bg-surface3 text-text" : "text-faint"
            }`}
          >
            {option.label}
          </button>
        );
      })}
    </div>
  );
}

export function Divider() {
  return <div className="h-px w-full bg-line2" aria-hidden />;
}

/* --- paper quiz vocabulary (مرور and یک‌سؤال share it) ------------------- */

export type PaperOptionState = "idle" | "chosen" | "correct" | "wrong" | "muted";

const PAPER_OPTION_STYLES: Record<PaperOptionState, string> = {
  idle: "border-ink/15 bg-paper2 text-ink hover:bg-paper",
  chosen: "border-trace/60 bg-trace/10 text-ink",
  correct: "border-ok/60 bg-ok/12 text-ink",
  wrong: "border-bad/60 bg-bad/12 text-ink",
  muted: "border-ink/10 text-inkMuted opacity-80",
};

/** One answer option on a paper card: ink borders, numbered in Persian. */
export function PaperOption({
  label,
  index,
  state,
  disabled,
  onPick,
}: {
  label: string;
  index: number;
  state: PaperOptionState;
  disabled: boolean;
  onPick: () => void;
}) {
  return (
    <button
      type="button"
      onClick={onPick}
      disabled={disabled}
      className={`pressable flex w-full items-start gap-3 rounded-field border px-3.5 py-3 text-right text-[13px] leading-6 transition-colors ${PAPER_OPTION_STYLES[state]}`}
    >
      <span
        className={`mt-0.5 flex h-5 w-5 shrink-0 items-center justify-center rounded-full border text-[10px] ${
          state === "correct"
            ? "border-okInk bg-okInk text-paper"
            : state === "wrong"
              ? "border-badInk bg-badInk text-paper"
              : "border-ink/25 text-inkMuted"
        }`}
        aria-hidden
      >
        {state === "correct" ? (
          <Icon name="check" size={12} strokeWidth={3} />
        ) : state === "wrong" ? (
          <Icon name="close" size={12} strokeWidth={3} />
        ) : (
          toFa(index + 1)
        )}
      </span>
      <span className="flex-1">{label}</span>
    </button>
  );
}
