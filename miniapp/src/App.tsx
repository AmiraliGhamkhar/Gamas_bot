import { useEffect, useState } from "react";
import { api, MeResponse, StudyAsset, StudyPack } from "./api";

type Tab = "account" | "library" | "review" | "explore";

function MyAccountTab() {
  const [me, setMe] = useState<MeResponse | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api
      .getMe()
      .then(setMe)
      .catch((e: Error) => setError(e.message));
  }, []);

  if (error) {
    return (
      <div className="rounded-xl border border-rose-800 bg-rose-950/40 p-4 text-sm text-rose-200">
        خطا در دریافت اطلاعات حساب: {error}
      </div>
    );
  }

  if (!me) {
    return <div className="p-4 text-sm text-slate-400">در حال بارگذاری…</div>;
  }

  const minutes = Math.floor(me.credit_seconds / 60);

  return (
    <div className="space-y-4">
      <h1 className="text-lg font-bold">حساب من</h1>
      <div className="grid grid-cols-2 gap-3">
        <div className="rounded-xl border border-slate-800 bg-slate-900 p-4">
          <div className="text-xs text-slate-400">اعتبار گاماس</div>
          <div className="mt-1 text-xl font-bold text-emerald-400">
            {minutes} دقیقه
          </div>
        </div>
        <div className="rounded-xl border border-slate-800 bg-slate-900 p-4">
          <div className="text-xs text-slate-400">امتیاز پاداش</div>
          <div className="mt-1 text-xl font-bold text-amber-400">
            {me.reward_points}
          </div>
        </div>
      </div>
      <div className="rounded-xl border border-slate-800 bg-slate-900 p-4 text-sm space-y-1">
        <div>شناسه تلگرام: {me.user_id}</div>
        <div>
          هدیه آزمون رایگان:{" "}
          {me.free_uses?.quiz ? "فعال" : "استفاده شده"}
        </div>
      </div>
    </div>
  );
}

function LibraryTab() {
  const [packs, setPacks] = useState<StudyPack[]>([]);
  const [selected, setSelected] = useState<{
    pack: StudyPack;
    assets: StudyAsset[];
  } | null>(null);
  const [newTitle, setNewTitle] = useState("");
  const [error, setError] = useState<string | null>(null);

  const load = () => {
    api
      .listPacks()
      .then((res) => setPacks(res.packs))
      .catch((e: Error) => setError(e.message));
  };

  useEffect(() => {
    load();
  }, []);

  const handleCreate = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!newTitle.trim()) return;
    try {
      await api.createPack(newTitle.trim());
      setNewTitle("");
      load();
    } catch (err) {
      setError((err as Error).message);
    }
  };

  const openPack = async (packId: number) => {
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
            بازگشت
          </button>
          <button
            onClick={() => handleDelete(selected.pack.pack_id)}
            className="text-sm text-rose-400"
          >
            حذف بسته
          </button>
        </div>
        <h2 className="text-lg font-bold">{selected.pack.title}</h2>
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
                  <div className="text-xs text-slate-400">{a.kind}</div>
                </div>
                <a
                  href={api.assetDownloadUrl(a.asset_id)}
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

  return (
    <div className="space-y-4">
      <h1 className="text-lg font-bold">کتابخانه</h1>
      {error && (
        <div className="rounded-xl border border-rose-800 bg-rose-950/40 p-3 text-xs text-rose-200">
          {error}
        </div>
      )}
      <form onSubmit={handleCreate} className="flex gap-2">
        <input
          value={newTitle}
          onChange={(e) => setNewTitle(e.target.value)}
          placeholder="نام بسته مطالعاتی جدید…"
          className="flex-1 rounded-xl border border-slate-800 bg-slate-900 px-3 py-2 text-sm text-slate-100 placeholder-slate-500 focus:border-sky-500 focus:outline-none"
        />
        <button
          type="submit"
          className="rounded-xl bg-sky-600 px-4 py-2 text-sm font-medium text-white"
        >
          ساخت
        </button>
      </form>
      <div className="space-y-2">
        {packs.length === 0 ? (
          <div className="rounded-xl border border-slate-800 bg-slate-900 p-4 text-sm text-slate-400">
            کتابخانه شما خالی است.
          </div>
        ) : (
          packs.map((p) => (
            <button
              key={p.pack_id}
              onClick={() => openPack(p.pack_id)}
              className="flex w-full items-center justify-between rounded-xl border border-slate-800 bg-slate-900 p-4 text-right"
            >
              <div>
                <div className="font-medium">{p.title}</div>
                <div className="text-xs text-slate-400">
                  {p.asset_count ?? 0} فایل
                </div>
              </div>
              <span className="text-xs text-slate-500">مشاهده</span>
            </button>
          ))
        )}
      </div>
    </div>
  );
}

function PlaceholderTab({ title }: { title: string }) {
  return (
    <div className="rounded-xl border border-slate-800 bg-slate-900 p-6 text-center">
      <h1 className="text-lg font-bold">{title}</h1>
      <p className="mt-2 text-sm text-slate-400">
        این بخش در فازهای بعدی فعال می‌شود.
      </p>
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
        {tab === "review" && <PlaceholderTab title="مرور" />}
        {tab === "explore" && <PlaceholderTab title="کاوش" />}
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
