import { useState } from "react";
import { ExploreSource } from "../../api";
import { openTelegramLink } from "../../lib/telegram";
import { haptic } from "../../lib/telegram";
import { Icon } from "../../ui/Icon";
import { toFa } from "../../lib/format";

/**
 * The academic backing of a card, folded away until asked for.
 *
 * Two rules matter more than the layout:
 *
 * 1. **Never invent a citation.** If the question has no verified source the
 *    panel says so plainly ("منبع این سؤال هنوز راستی‌آزمایی نشده است") instead of
 *    borrowing credibility it does not have. The server marks a source verified
 *    only when it carries a title, an institution, an https URL and a known
 *    evidence tier; anything less arrives as `null`.
 * 2. **Never hand the WebView an arbitrary link.** Only an `https:` URL is
 *    opened, and it opens through Telegram's own link handler so the Mini App
 *    keeps its viewport and back behaviour.
 */

const EVIDENCE_LABELS: Record<string, string> = {
  textbook: "کتاب درسی دانشگاهی",
  institutional: "منبع نهادی معتبر",
  educational: "منبع آموزشی معتبر",
};

function safeHttpsUrl(raw: string): string | null {
  try {
    const url = new URL(raw);
    if (url.protocol !== "https:" || !url.hostname) return null;
    if (url.username || url.password) return null;
    return url.toString();
  } catch {
    return null;
  }
}

/** The one-line citation shown even while the panel is closed. */
function citationLine(source: ExploreSource): string {
  const parts = [source.source_institution, source.source_title].filter(Boolean);
  return parts.join(" · ");
}

export function SourcePanel({
  source,
  onOpen,
}: {
  source: ExploreSource | null;
  /** Called once, when the reader actually expands the citation. */
  onOpen?: () => void;
}) {
  const [open, setOpen] = useState(false);
  const verifiedUrl = source ? safeHttpsUrl(source.source_url) : null;

  if (!source) {
    return (
      <p className="flex items-center gap-1.5 rounded-field border border-dashed border-ink/20 px-3 py-2 text-[11px] leading-5 text-inkMuted">
        <Icon name="info" size={13} className="shrink-0" />
        منبع این سؤال هنوز راستی‌آزمایی نشده است؛ به‌زودی منبع معتبر آن اضافه می‌شود.
      </p>
    );
  }

  const evidence = EVIDENCE_LABELS[source.evidence_level] ?? "";

  const toggle = () => {
    if (!open) onOpen?.();
    haptic("tap");
    setOpen((prev) => !prev);
  };

  return (
    <div className="rounded-field border border-ink/12 bg-paper2">
      <button
        type="button"
        onClick={toggle}
        aria-expanded={open}
        className="pressable flex min-h-[44px] w-full items-center gap-2 px-3 py-2 text-right"
      >
        <Icon name="library" size={14} className="shrink-0 text-trace" />
        <span className="min-w-0 flex-1">
          <span className="block text-[11px] font-medium text-trace">منبع علمی</span>
          <span className="mt-0.5 block truncate text-[11px] leading-5 text-inkMuted">
            {citationLine(source)}
          </span>
        </span>
        <Icon
          name={open ? "chevron-up" : "chevron-down"}
          size={14}
          className="shrink-0 text-inkMuted"
        />
      </button>

      {open && (
        <div className="animate-fade space-y-1.5 border-t border-ink/10 px-3 py-2.5 text-[11px] leading-6 text-inkMuted">
          <p className="text-[12px] font-medium leading-6 text-ink">{source.source_title}</p>
          {source.source_author && <p>نویسنده: {source.source_author}</p>}
          <p>ناشر: {source.source_institution}</p>
          {source.source_year && <p className="nums">سال: {toFa(source.source_year)}</p>}
          {source.source_page_or_section && <p>{source.source_page_or_section}</p>}
          {evidence && (
            <p className="flex items-center gap-1.5">
              <Icon name="check" size={12} className="text-okInk" />
              {evidence}
            </p>
          )}
          {verifiedUrl ? (
            <button
              type="button"
              onClick={() => {
                haptic("tap");
                openTelegramLink(verifiedUrl);
              }}
              className="pressable mt-0.5 inline-flex min-h-[36px] items-center gap-1.5 rounded-pill border border-trace/40 px-3 text-[11px] font-medium text-trace"
            >
              <Icon name="forward" size={13} />
              مشاهده منبع
            </button>
          ) : (
            <p className="text-[11px] text-inkMuted">نشانی این منبع در دسترس نیست.</p>
          )}
        </div>
      )}
    </div>
  );
}
