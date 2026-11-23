export interface MeResponse {
  user_id: number;
  credit_seconds: number;
  reward_points: number;
  free_uses: { quiz?: boolean };
  banned: boolean;
}

export interface SubscriptionResponse {
  required_channel: string | null;
}

export interface ReferralsResponse {
  invited_count: number;
}

export interface PaymentItem {
  payment_id: number;
  amount_toman: number;
  status: string;
  created_at: string;
}

export interface RewardCatalogItem {
  code: string;
  title: string;
  cost_points: number;
  credit_seconds: number;
}

export interface ReviewGroup {
  session_id: number;
  item_id: number | null;
  question_count: number;
  status: string;
  created_at: string;
}

export interface ReviewQuestion {
  question_id: number;
  session_id: number;
  idx: number;
  question: string;
  options: string[];
}

export interface ReviewAnswerResult {
  is_correct: boolean;
  correct_index: number;
  explanation: string;
}

export interface ExploreItem {
  question_id: number;
  topic: string;
  difficulty: string;
  question_text: string;
  options: string[];
}

export interface ExploreAnswerResult {
  is_correct: boolean;
  correct_index: number;
  explanation: string;
  points_awarded: number;
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
  getMe: () => apiFetch<MeResponse>("/api/v1/me"),
  getSubscription: () => apiFetch<SubscriptionResponse>("/api/v1/me/subscription"),
  getReferrals: () => apiFetch<ReferralsResponse>("/api/v1/me/referrals"),
  getPayments: () => apiFetch<{ payments: PaymentItem[] }>("/api/v1/me/payments"),
  getRewardCatalog: () => apiFetch<{ catalog: RewardCatalogItem[] }>("/api/v1/rewards/catalog"),
  redeemReward: (reward_code: string) =>
    apiFetch<{ balance: number; credit_seconds_granted: number }>("/api/v1/rewards/redeem", {
      method: "POST",
      body: JSON.stringify({ reward_code }),
    }),
  listPacks: () => apiFetch<{ packs: StudyPack[] }>("/api/v1/library"),
  createPack: (title: string) =>
    apiFetch<{ pack: StudyPack }>("/api/packs", {
      method: "POST",
      body: JSON.stringify({ title }),
    }),
  getPack: (packId: number) =>
    apiFetch<{ pack: StudyPack; assets: StudyAsset[] }>(`/api/v1/library/${packId}`),
  deletePack: (packId: number) =>
    apiFetch<{ deleted: boolean }>(`/api/v1/library/${packId}`, { method: "DELETE" }),
  assetDownloadUrl: (packId: number, assetId: number) =>
    `/api/v1/library/${packId}/download/${assetId}`,
  getExploreFeed: () => apiFetch<{ items: ExploreItem[] }>("/api/v1/explore/feed"),
  submitExploreAnswer: (question_id: number, chosen_index: number, dwell_ms: number) =>
    apiFetch<ExploreAnswerResult>("/api/v1/explore/answers", {
      method: "POST",
      body: JSON.stringify({ question_id, chosen_index, dwell_ms }),
    }),
  sendExploreEngagement: (question_id: number, dwell_ms: number) =>
    apiFetch<{ recorded: boolean }>("/api/v1/explore/engagement", {
      method: "POST",
      body: JSON.stringify({ question_id, dwell_ms }),
    }),
};
