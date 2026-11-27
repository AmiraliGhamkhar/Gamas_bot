/**
 * The single gateway to the Gamas backend.
 *
 * Every request is authenticated with Telegram `initData`; the server is the
 * only authority on who the user is, what they own and what anything costs.
 * Nothing in this file derives a balance, a price, a reward or a ranking.
 */

import { getInitData } from "./lib/telegram";

export interface MeResponse {
  user_id: number;
  credit_seconds: number;
  reward_points: number;
  free_uses: { quiz?: boolean };
  banned: boolean;
  /** Bot username (no @) — invite, share, support and purchase links use it. */
  bot_username: string;
}

export interface SubscriptionResponse {
  required_channel: string | null;
  channel_url: string | null;
  required: boolean;
}

export interface ReferralsResponse {
  invited_count: number;
}

export interface PaymentItem {
  payment_id: number;
  plan_code: string;
  amount_toman: number;
  status: string;
  created_at: string;
  purchased_seconds: number | null;
}

export interface PlanOption {
  code: string;
  hours: number;
  price: number;
  original_price: number | null;
  validity_days: number | null;
}

export interface PlansResponse {
  plans: PlanOption[];
}

export interface CurrentPlan {
  code: string | null;
  hours: number | null;
  price: number | null;
  original_price: number | null;
  validity_days: number | null;
  purchased_at: string | null;
}

export interface PlanSummaryResponse {
  unlimited: boolean;
  credit_seconds: number;
  expires_at: string | null;
  plan: CurrentPlan | null;
}

export interface RewardCatalogItem {
  code: string;
  title: string;
  cost_points: number;
  credit_seconds: number;
}

// What the server actually committed. `credited` is false for catalog entries
// that grant no processing credit, and `credit_seconds_granted` is only non-zero
// once the credit ledger row exists.
export interface RedeemResponse {
  balance: number;
  credit_seconds_granted: number;
  credited: boolean;
  reward_code: string;
  redemption_id: number;
  ledger_id: number | null;
  available_seconds: number;
  idempotent: boolean;
}

// Review is grouped by the Study Pack that produced the questions. A group is
// either a pack ("pack:<id>") or, for questions generated before packs existed,
// their real library row ("legacy:<item_id>").
export interface ReviewGroup {
  group_key: string;
  pack_id: number | null;
  title: string;
  source_type: string;
  processing_status: string;
  legacy: boolean;
  question_count: number;
  answered_count: number;
  correct_count: number;
  latest_at: string;
}

export interface ReviewAnsweredState {
  chosen_index: number;
  is_correct: boolean;
  answered_at: string;
  correct_index: number;
  explanation: string;
}

export type PublicationStatus = "pending" | "approved" | "rejected" | null;

export interface ReviewQuestion {
  question_id: number;
  session_id: number;
  group_key: string;
  pack_id: number | null;
  pack_title: string;
  legacy: boolean;
  idx: number;
  question: string;
  options: string[];
  answered: ReviewAnsweredState | null;
  publication_status: PublicationStatus;
}

export interface ReviewAnswerResult {
  is_correct: boolean;
  correct_index: number;
  explanation: string;
  already_answered: boolean;
}

export interface ReviewShareResult {
  shared: boolean;
  explore_question_id: number;
  status: string;
  already_shared: boolean;
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
  submission_id?: number | null;
  document_id?: number | null;
  source_item_id?: number | null;
  source_filename?: string | null;
  error_kind?: string | null;
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

export interface PackDetailResponse {
  pack: StudyPack;
  assets: StudyAsset[];
  /** Questions this pack owns; the Review action is offered only when > 0. */
  question_count: number;
}

export class ApiError extends Error {
  status: number;
  constructor(message: string, status: number) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(path, {
    ...init,
    headers: {
      "Content-Type": "application/json",
      "X-Telegram-Init-Data": getInitData(),
      ...(init?.headers ?? {}),
    },
  });
  if (!res.ok) {
    const payload = (await res.json().catch(() => null)) as { error?: string } | null;
    throw new ApiError(payload?.error || `HTTP ${res.status}`, res.status);
  }
  return (await res.json()) as T;
}

/** RFC 5987-aware filename from Content-Disposition, or null. */
function filenameFromDisposition(value: string | null): string | null {
  if (!value) return null;
  const extended = /filename\*\s*=\s*UTF-8''([^;]+)/i.exec(value);
  if (extended) {
    try {
      return decodeURIComponent(extended[1].trim());
    } catch {
      /* fall through to the plain form */
    }
  }
  const plain = /filename\s*=\s*"([^"]+)"|filename\s*=\s*([^;]+)/i.exec(value);
  return plain ? (plain[1] ?? plain[2]).trim() : null;
}

/** Binary assets need the same auth header, so they are fetched, not linked. */
async function download(
  path: string,
): Promise<{ blob: Blob; filename: string | null }> {
  const res = await fetch(path, {
    headers: { "X-Telegram-Init-Data": getInitData() },
  });
  if (!res.ok) {
    const payload = (await res.json().catch(() => null)) as { error?: string } | null;
    throw new ApiError(payload?.error || `HTTP ${res.status}`, res.status);
  }
  return {
    blob: await res.blob(),
    // The server already chose a safe filename with its extension; the caller
    // falls back to a constructed name only when the header is missing.
    filename: filenameFromDisposition(res.headers.get("Content-Disposition")),
  };
}

export const api = {
  getMe: () => request<MeResponse>("/api/v1/me"),
  getSubscription: () => request<SubscriptionResponse>("/api/v1/me/subscription"),
  getPlan: () => request<PlanSummaryResponse>("/api/v1/me/plan"),
  getPlans: () => request<PlansResponse>("/api/v1/plans"),
  getReferrals: () => request<ReferralsResponse>("/api/v1/me/referrals"),
  getPayments: () => request<{ payments: PaymentItem[] }>("/api/v1/me/payments"),
  getRewardCatalog: () =>
    request<{ catalog: RewardCatalogItem[] }>("/api/v1/rewards/catalog"),
  // A redemption is idempotent per `idempotency_key`: a retry (double tap,
  // flaky network) returns the original result instead of spending again.
  redeemReward: (reward_code: string, idempotency_key?: string) =>
    request<RedeemResponse>("/api/v1/rewards/redeem", {
      method: "POST",
      body: JSON.stringify({ reward_code, idempotency_key }),
    }),

  // The first page keeps the bare URL (older contracts and test mocks match
  // it exactly); explicit paging appends clamped limit/offset query params.
  listPacks: (limit?: number, offset?: number) =>
    request<{
      packs: StudyPack[];
      total?: number;
      limit?: number;
      offset?: number;
    }>(
      limit === undefined
        ? "/api/v1/library"
        : `/api/v1/library?limit=${limit}&offset=${offset ?? 0}`,
    ),
  getPack: (packId: number) =>
    request<PackDetailResponse>(`/api/v1/library/${packId}`),
  deletePack: (packId: number) =>
    request<{ deleted: boolean }>(`/api/v1/library/${packId}`, { method: "DELETE" }),
  downloadAsset: (packId: number, assetId: number) =>
    download(`/api/v1/library/${packId}/download/${assetId}`),

  getReviewGroups: (limit = 200) =>
    request<{ groups: ReviewGroup[]; total: number }>(
      `/api/v1/review/groups?limit=${limit}`,
    ),
  getReviewQuestions: (groupKey: string, limit = 200) =>
    request<{ questions: ReviewQuestion[]; total: number }>(
      `/api/v1/review/questions?group_key=${encodeURIComponent(groupKey)}&limit=${limit}`,
    ),
  submitReviewAnswer: (question_id: number, chosen_index: number) =>
    request<ReviewAnswerResult>("/api/v1/review/answers", {
      method: "POST",
      body: JSON.stringify({ question_id, chosen_index }),
    }),
  shareReviewQuestion: (question_id: number) =>
    request<ReviewShareResult>("/api/v1/review/share", {
      method: "POST",
      body: JSON.stringify({ question_id }),
    }),

  getExploreFeed: () => request<{ items: ExploreItem[] }>("/api/v1/explore/feed"),
  submitExploreAnswer: (question_id: number, chosen_index: number, dwell_ms: number) =>
    request<ExploreAnswerResult>("/api/v1/explore/answers", {
      method: "POST",
      body: JSON.stringify({ question_id, chosen_index, dwell_ms }),
    }),
  sendExploreEngagement: (question_id: number, dwell_ms: number) =>
    request<{ recorded: boolean }>("/api/v1/explore/engagement", {
      method: "POST",
      body: JSON.stringify({ question_id, dwell_ms }),
    }),
};

/** Save a fetched blob through the browser (Telegram WebView friendly). */
export function saveBlob(blob: Blob, filename: string): void {
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  link.rel = "noopener";
  document.body.appendChild(link);
  link.click();
  link.remove();
  // Give the WebView a moment to start the download before revoking the URL.
  setTimeout(() => URL.revokeObjectURL(url), 10_000);
}
