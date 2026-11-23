export interface MeResponse {
  user_id: number;
  credit_seconds: number;
  reward_points: number;
  free_uses: { quiz?: boolean };
  banned: boolean;
}

export interface StudyPack {
  pack_id: number;
  user_id: number;
  title: string;
  source_type: string;
  status: string;
  created_at: string;
  updated_at: string;
  asset_count?: number;
}

export interface StudyAsset {
  asset_id: number;
  pack_id: number;
  kind: string;
  title: string;
  mime_type: string;
  byte_size: number;
  created_at: string;
}

function getInitData(): string {
  const tg = (window as unknown as { Telegram?: { WebApp?: { initData?: string } } }).Telegram;
  return tg?.WebApp?.initData ?? "";
}

async function apiFetch<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(path, {
    ...init,
    headers: {
      "Content-Type": "application/json",
      "X-Telegram-Init-Data": getInitData(),
      ...(init?.headers ?? {}),
    },
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({ error: res.statusText }));
    throw new Error(err.error || `HTTP ${res.status}`);
  }
  return res.json() as Promise<T>;
}

export const api = {
  getMe: () => apiFetch<MeResponse>("/api/me"),
  listPacks: () => apiFetch<{ packs: StudyPack[] }>("/api/packs"),
  createPack: (title: string) =>
    apiFetch<{ pack: StudyPack }>("/api/packs", {
      method: "POST",
      body: JSON.stringify({ title }),
    }),
  getPack: (packId: number) =>
    apiFetch<{ pack: StudyPack; assets: StudyAsset[] }>(`/api/packs/${packId}`),
  deletePack: (packId: number) =>
    apiFetch<{ deleted: boolean }>(`/api/packs/${packId}`, { method: "DELETE" }),
  assetDownloadUrl: (assetId: number) => `/api/assets/${assetId}/download`,
};
