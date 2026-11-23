import type { ReactNode } from "react";
import { Icon, IconName } from "./Icon";
import { Button } from "./primitives";

/** A skeleton that mirrors the real card rhythm instead of a spinner blob. */
export function ListSkeleton({ rows = 3 }: { rows?: number }) {
  return (
    <div className="space-y-3" aria-busy="true" aria-live="polite">
      <span className="sr-only">در حال بارگذاری…</span>
      {Array.from({ length: rows }).map((_, index) => (
        <div key={index} className="card space-y-3 p-4">
          <div className="skeleton h-4 w-2/3" />
          <div className="skeleton h-3 w-1/3" />
        </div>
      ))}
    </div>
  );
}

export function EmptyState({
  icon,
  title,
  description,
  action,
}: {
  icon: IconName;
  title: string;
  description?: string;
  action?: ReactNode;
}) {
  return (
    <div className="card flex flex-col items-center gap-3 px-6 py-10 text-center">
      <span className="flex h-12 w-12 items-center justify-center rounded-full bg-surface2 text-faint">
        <Icon name={icon} size={22} />
      </span>
      <div className="space-y-1">
        <p className="text-sm font-medium text-text">{title}</p>
        {description && (
          <p className="mx-auto max-w-[28ch] text-xs leading-6 text-faint">
            {description}
          </p>
        )}
      </div>
      {action}
    </div>
  );
}

export function ErrorState({
  message,
  onRetry,
  compact = false,
}: {
  message: string;
  onRetry?: () => void;
  compact?: boolean;
}) {
  return (
    <div
      role="alert"
      className={`card border-bad/25 bg-badSoft/40 ${compact ? "p-3" : "p-4"}`}
    >
      <div className="flex items-start gap-2.5">
        <Icon name="alert" size={18} className="mt-0.5 shrink-0 text-bad" />
        <div className="min-w-0 flex-1 space-y-2">
          <p className="text-xs leading-6 text-bad">{message}</p>
          {onRetry && (
            <Button variant="ghost" icon="refresh" onClick={onRetry} className="px-0">
              تلاش مجدد
            </Button>
          )}
        </div>
      </div>
    </div>
  );
}

export function InlineNote({ children }: { children: ReactNode }) {
  return (
    <p className="rounded-field bg-surface2 px-3 py-2 text-[11px] leading-6 text-faint">
      {children}
    </p>
  );
}
