import { useEffect, useMemo, useState } from "react";
import { api, saveBlob, StudyAsset, StudyPack } from "../../api";
import { formatDate, formatRelative, formatSize, toFa } from "../../lib/format";
import { haptic } from "../../lib/telegram";
import { describeError, useAsync } from "../../lib/useAsync";
import { EcgLoader } from "../../ui/Ecg";
import { Icon, IconName } from "../../ui/Icon";
import {
  Button,
  Chip,
  Group,
  Row,
  ScreenHeader,
  Segmented,
} from "../../ui/primitives";
import { EmptyState, ErrorState, InlineNote, ListSkeleton } from "../../ui/states";

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

const STATUS_LABELS: Record<string, { label: string; tone: "neutral" | "brand" | "ok" | "bad" }> = {
  pending: { label: "در انتظار", tone: "neutral" },
  queued: { label: "در صف", tone: "neutral" },
  processing: { label: "در حال پردازش", tone: "brand" },
  running: { label: "در حال پردازش", tone: "brand" },
  done: { label: "آماده", tone: "ok" },
  completed: { label: "آماده", tone: "ok" },
  partial: { label: "آماده با هشدار", tone: "brand" },
  failed: { label: "ناموفق", tone: "bad" },
  rejected: { label: "ردشده", tone: "bad" },
};

const ASSET_META: Record<string, { label: string; icon: IconName }> = {
  booklet: { label: "جزوهٔ PDF", icon: "doc" },
  docx: { label: "نسخهٔ Word", icon: "doc" },
  transcript: { label: "متن خام", icon: "text" },
  txt: { label: "متن خام (TXT)", icon: "text" },
  keypoints: { label: "نکات کلیدی", icon: "spark" },
  review_html: { label: "مرور تعاملی", icon: "cards" },
  quiz: { label: "سؤال‌ها", icon: "review" },
  original: { label: "فایل اصلی", icon: "doc" },
  audio: { label: "صوت", icon: "audio" },
  video: { label: "ویدیو", icon: "video" },
  pdf: { label: "PDF", icon: "doc" },
  sheet: { label: "صفحه‌گسترده", icon: "sheet" },
};

function assetMeta(kind: string): { label: string; icon: IconName } {
  return ASSET_META[kind] ?? { label: kind, icon: "doc" };
}

const MIME_EXTENSIONS: Record<string, string> = {
  "application/pdf": ".pdf",
  "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
  "application/vnd.openxmlformats-officedocument.presentationml.presentation": ".pptx",
  "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
  "text/plain": ".txt",
  "text/html": ".html",
  "text/csv": ".csv",
  "audio/mpeg": ".mp3",
  "audio/ogg": ".ogg",
  "video/mp4": ".mp4",
  "application/zip": ".zip",
};

/**
 * Fallback download name when the server sent no filename: the Persian asset
 * title alone has no extension, so the OS would not know how to open it.
 */
function fallbackAssetName(asset: StudyAsset): string {
  const base =
    (asset.title || "").replace(/[\\/:*?"<>|]/g, "_").trim() ||
    `gamas-${asset.kind}`;
  if (/\.[A-Za-z0-9]{1,8}$/.test(base)) return base;
  const ext = MIME_EXTENSIONS[(asset.mime_type || "").split(";")[0].trim()] ?? "";
  return ext ? `${base}${ext}` : base;
}

function sourceLabel(pack: StudyPack): string {
  return SOURCE_LABELS[pack.source_type] ?? pack.source_type;
}

function statusMeta(pack: StudyPack) {
  return STATUS_LABELS[pack.status] ?? { label: pack.status, tone: "neutral" as const };
}

function PackRow({
  pack,
  onOpen,
  index,
}: {
  pack: StudyPack;
  onOpen: () => void;
  index: number;
}) {
  const status = statusMeta(pack);
  return (
    <button
      type="button"
      onClick={onOpen}
      style={{ animationDelay: `${Math.min(index, 6) * 40}ms` }}
      className="pressable flex w-full items-center gap-3 px-4 py-3.5 text-right"
    >
      <Icon
        name={pack.source_type === "audio" ? "audio" : pack.source_type === "video" ? "video" : "doc"}
        size={20}
        className="shrink-0 text-faint"
      />
      <span className="min-w-0 flex-1">
        <span className="block truncate text-sm font-medium text-text">{pack.title}</span>
        <span className="mt-1 flex flex-wrap items-center gap-x-2 gap-y-1 text-[11px] text-faint">
          <span>{sourceLabel(pack)}</span>
          <span aria-hidden>·</span>
          <span>{formatRelative(pack.created_at)}</span>
          <span aria-hidden>·</span>
          <span className="nums">{toFa(pack.asset_count ?? 0)} فایل</span>
        </span>
      </span>
      <span className="flex shrink-0 items-center gap-2">
        <Chip tone={status.tone} dot={status.tone !== "ok"}>
          {status.label}
        </Chip>
        <Icon name="forward" size={16} className="text-faint" />
      </span>
    </button>
  );
}

function AssetRow({
  asset,
  packId,
  onError,
}: {
  asset: StudyAsset;
  packId: number;
  onError: (message: string) => void;
}) {
  const [busy, setBusy] = useState(false);
  const meta = assetMeta(asset.kind);

  const download = async () => {
    setBusy(true);
    haptic("tap");
    try {
      const result = await api.downloadAsset(packId, asset.asset_id);
      saveBlob(result.blob, result.filename || fallbackAssetName(asset));
      haptic("success");
    } catch (error) {
      haptic("error");
      onError(describeError(error) || "دریافت فایل انجام نشد.");
    } finally {
      setBusy(false);
    }
  };

  return (
    <Row
      icon={meta.icon}
      title={asset.title || meta.label}
      meta={`${meta.label} · ${formatSize(asset.byte_size)}`}
      trailing={
        <Button
          variant="ghost"
          icon={busy ? "refresh" : "download"}
          onClick={download}
          disabled={busy}
          className="px-2.5 text-xs"
          aria-label={`دانلود ${asset.title || meta.label}`}
        >
          {busy ? "…" : "دانلود"}
        </Button>
      }
    />
  );
}

function PackDetail({
  packId,
  onBack,
  onOpenReview,
}: {
  packId: number;
  onBack: () => void;
  onOpenReview: (packId: number) => void;
}) {
  const detail = useAsync(() => api.getPack(packId), [packId]);
  const [actionError, setActionError] = useState<string | null>(null);
  const [confirmDelete, setConfirmDelete] = useState(false);
  const [deleting, setDeleting] = useState(false);

  const pack = detail.data?.pack ?? null;

  // While the pipeline is working on the pack, this screen is the progress view:
  // one quiet re-read every few seconds, and nothing once it settles.
  const pendingStatus =
    pack !== null &&
    ["pending", "queued", "processing", "running"].includes(pack.status);
  useEffect(() => {
    if (!pendingStatus) return;
    const timer = setTimeout(detail.reload, 8000);
    return () => clearTimeout(timer);
  }, [pendingStatus, detail.data, detail.reload]);

  const assets = detail.data?.assets ?? [];
  const questionCount = detail.data?.question_count ?? 0;
  const status = pack ? statusMeta(pack) : null;

  const remove = async () => {
    setDeleting(true);
    try {
      await api.deletePack(packId);
      haptic("success");
      onBack();
    } catch (error) {
      haptic("error");
      setActionError(describeError(error) || "حذف بسته انجام نشد.");
      setDeleting(false);
      setConfirmDelete(false);
    }
  };

  return (
    <div className="space-y-4">
      <ScreenHeader
        title={pack?.title ?? "بستهٔ مطالعاتی"}
        onBack={onBack}
        subtitle={
          pack ? (
            <span className="flex flex-wrap items-center gap-x-2 gap-y-1">
              <span>{sourceLabel(pack)}</span>
              <span aria-hidden>·</span>
              <span>{formatDate(pack.created_at)}</span>
              {pack.source_filename && (
                <>
                  <span aria-hidden>·</span>
                  <span className="truncate" dir="ltr">
                    {pack.source_filename}
                  </span>
                </>
              )}
            </span>
          ) : undefined
        }
        actions={status ? <Chip tone={status.tone} dot={status.tone !== "ok"}>{status.label}</Chip> : undefined}
      />

      {detail.loading && <ListSkeleton rows={2} />}
      {detail.error && <ErrorState message={detail.error} onRetry={detail.reload} />}
      {actionError && <ErrorState compact message={actionError} />}

      {pack && !detail.loading && !detail.error && (
        <>
          {pack.status === "processing" && (
            <div className="space-y-3 rounded-card border border-line bg-surface p-4">
              <p className="text-xs text-muted">
                این بسته در حال آماده‌شدن است: بازکردن فایل، رونویسی و ساخت جزوه و
                سؤال‌ها.
              </p>
              <EcgLoader label="در حال پردازش بسته" />
              <InlineNote>فایل‌ها به‌تدریج در همین صفحه ظاهر می‌شوند.</InlineNote>
            </div>
          )}
          {pack.status === "failed" && (
            <div className="rounded-card border border-bad/25 bg-badSoft/40 p-4">
              <p className="text-xs leading-6 text-bad">
                پردازش این بسته کامل نشد. فایل اصلی را دوباره در ربات بفرستید تا نسخهٔ
                سالم ساخته شود.
              </p>
            </div>
          )}

          <Group title={`فایل‌ها${assets.length > 0 ? ` (${toFa(assets.length)})` : ""}`}>
            {assets.length === 0 ? (
              <div className="px-4 py-3">
                <InlineNote>
                  هنوز فایلی برای این بسته آماده نشده است. پس از پایان پردازش، جزوه، متن
                  خام و سایر خروجی‌ها این‌جا فهرست می‌شوند.
                </InlineNote>
              </div>
            ) : (
              assets.map((asset) => (
                <AssetRow
                  key={asset.asset_id}
                  asset={asset}
                  packId={packId}
                  onError={setActionError}
                />
              ))
            )}
          </Group>

          {questionCount > 0 && (
            <Button
              block
              variant="primary"
              icon="review"
              onClick={() => {
                haptic("tap");
                onOpenReview(packId);
              }}
            >
              مرور {toFa(questionCount)} سؤال این بسته
            </Button>
          )}

          <div className="flex justify-center pt-2">
            {confirmDelete ? (
              <div className="flex items-center gap-2">
                <Button variant="danger" onClick={remove} disabled={deleting}>
                  {deleting ? "در حال حذف…" : "تأیید حذف"}
                </Button>
                <Button variant="ghost" onClick={() => setConfirmDelete(false)}>
                  انصراف
                </Button>
              </div>
            ) : (
              <Button
                variant="ghost"
                icon="trash"
                onClick={() => setConfirmDelete(true)}
                className="!text-faint hover:!text-bad"
              >
                حذف این بسته
              </Button>
            )}
          </div>
        </>
      )}
    </div>
  );
}

export function LibraryTab({ onOpenReview }: { onOpenReview: (packId: number) => void }) {
  const packs = useAsync(api.listPacks, []);
  const [openPackId, setOpenPackId] = useState<number | null>(null);
  const [query, setQuery] = useState("");
  const [order, setOrder] = useState<"newest" | "oldest">("newest");

  const visible = useMemo(() => {
    const list = packs.data?.packs ?? [];
    const needle = query.trim().toLowerCase();
    const filtered = needle
      ? list.filter(
          (pack) =>
            pack.title.toLowerCase().includes(needle) ||
            sourceLabel(pack).toLowerCase().includes(needle) ||
            (pack.source_filename ?? "").toLowerCase().includes(needle),
        )
      : list;
    return [...filtered].sort((a, b) => {
      const cmp = String(a.created_at).localeCompare(String(b.created_at));
      return order === "newest" ? -cmp : cmp;
    });
  }, [packs.data, query, order]);

  if (openPackId !== null) {
    return (
      <PackDetail
        packId={openPackId}
        onBack={() => setOpenPackId(null)}
        onOpenReview={(packId) => {
          setOpenPackId(null);
          onOpenReview(packId);
        }}
      />
    );
  }

  if (packs.loading && !packs.data) return <ListSkeleton rows={4} />;
  if (packs.error && !packs.data) {
    return <ErrorState message={packs.error} onRetry={packs.reload} />;
  }

  const total = packs.data?.packs.length ?? 0;

  return (
    <div className="space-y-4">
      <ScreenHeader
        title="کتابخانه"
        actions={
          <button
            type="button"
            onClick={() => {
              haptic("tap");
              packs.reload();
            }}
            aria-label="به‌روزرسانی کتابخانه"
            className="pressable flex h-10 w-10 items-center justify-center rounded-full text-muted hover:bg-surface2"
          >
            <Icon name="refresh" size={18} className={packs.loading ? "animate-spin" : ""} />
          </button>
        }
        subtitle={
          total > 0 ? (
            <span className="nums">{toFa(total)} بستهٔ مطالعاتی</span>
          ) : (
            "هر فایلی که به گاماس می‌فرستید این‌جا یک بسته می‌شود."
          )
        }
      />

      {total === 0 ? (
        <EmptyState
          emblem="book"
          title="کتابخانه خالی است"
          description="یک فایل صوتی، ویدیویی یا سند در ربات بفرستید تا بستهٔ مطالعاتی و خروجی‌هایش این‌جا ساخته شوند."
        />
      ) : (
        <>
          <div className="space-y-2">
            <label className="relative block">
              <span className="sr-only">جستجو در بسته‌ها</span>
              <Icon
                name="search"
                size={17}
                className="pointer-events-none absolute right-3 top-1/2 -translate-y-1/2 text-faint"
              />
              <input
                value={query}
                onChange={(event) => setQuery(event.target.value)}
                placeholder="جستجو در بسته‌ها"
                className="min-h-[44px] w-full rounded-field border border-line bg-surface pr-10 pl-3 text-base text-text placeholder:text-faint focus:border-brand/50 focus:outline-none"
              />
            </label>
            <div className="flex items-center justify-between px-0.5">
              <span className="text-[11px] text-faint">
                {query.trim() ? `${toFa(visible.length)} نتیجه` : "همهٔ بسته‌ها"}
              </span>
              <Segmented
                ariaLabel="ترتیب نمایش"
                value={order}
                onChange={setOrder}
                options={[
                  { value: "newest", label: "جدیدترین" },
                  { value: "oldest", label: "قدیمی‌ترین" },
                ]}
              />
            </div>
          </div>

          {visible.length === 0 ? (
            <EmptyState
              emblem="leaf"
              title="بسته‌ای با این عنوان پیدا نشد"
              description="عبارت دیگری را جستجو کنید."
            />
          ) : (
            <Group listClassName="stagger">
              {visible.map((pack, index) => (
                <PackRow
                  key={pack.pack_id}
                  pack={pack}
                  index={index}
                  onOpen={() => {
                    haptic("tap");
                    setOpenPackId(pack.pack_id);
                  }}
                />
              ))}
            </Group>
          )}
        </>
      )}
    </div>
  );
}
