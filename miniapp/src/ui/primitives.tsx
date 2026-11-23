import type { ButtonHTMLAttributes, ReactNode } from "react";
import { Icon, IconName } from "./Icon";

export function Card({
  children,
  className = "",
  as: Tag = "div",
}: {
  children: ReactNode;
  className?: string;
  as?: "div" | "section" | "article";
}) {
  return <Tag className={`card p-4 ${className}`}>{children}</Tag>;
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

export function Chip({
  children,
  tone = "neutral",
  dot = false,
  className = "",
}: {
  children: ReactNode;
  tone?: Tone;
  dot?: boolean;
  className?: string;
}) {
  return (
    <span
      className={`inline-flex items-center gap-1.5 rounded-pill border px-2.5 py-1 text-[11px] font-medium ${TONES[tone]} ${className}`}
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
