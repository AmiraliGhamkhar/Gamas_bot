import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import App from "../App";
import { describeError } from "../lib/useAsync";
import { ApiError } from "../api";

/**
 * Reward redemption must be retry-safe: one in-flight request at a time, and a
 * retried attempt re-sends the same idempotency key so the server can replay
 * instead of spending twice. Errors the user reads are Persian, never a raw
 * browser string like "Failed to fetch".
 */

const ME = {
  user_id: 4242,
  credit_seconds: 5400,
  reward_points: 120,
  free_uses: { quiz: false },
  banned: false,
  bot_username: "GamasBot",
};

const PLAN = { unlimited: false, credit_seconds: 5400, expires_at: null, plan: null };
const PLANS = {
  plans: [{ code: "h5", hours: 5, price: 49000, original_price: null, validity_days: 30 }],
};
const CATALOG = {
  catalog: [
    { code: "credit_30", title: "۳۰ دقیقه اعتبار", cost_points: 100, credit_seconds: 1800 },
  ],
};

const ROUTES: Record<string, unknown> = {
  "/api/v1/me": ME,
  "/api/v1/me/plan": PLAN,
  "/api/v1/plans": PLANS,
  "/api/v1/me/referrals": { invited_count: 0 },
  "/api/v1/me/payments": { payments: [] },
  "/api/v1/me/subscription": { required_channel: null, channel_url: null, required: false },
  "/api/v1/rewards/catalog": CATALOG,
  "/api/v1/review/groups?limit=200": { groups: [], total: 0 },
};

const REDEEM_OK = {
  balance: 20,
  credit_seconds_granted: 1800,
  credited: true,
  reward_code: "credit_30",
  redemption_id: 1,
  ledger_id: 2,
  available_seconds: 7200,
  idempotent: false,
};

interface RedeemBody {
  reward_code?: string;
  idempotency_key?: string;
}

function signIn() {
  (window as unknown as { Telegram: unknown }).Telegram = {
    WebApp: {
      initData: "auth_date=1&hash=stub",
      initDataUnsafe: { user: { first_name: "سارا", username: "sara" } },
    },
  };
}

async function openWalletRedeem() {
  const user = userEvent.setup();
  render(<App />);
  await user.click(await screen.findByRole("tab", { name: "حساب من" }));
  await user.click(await screen.findByText("کیف پول"));
  return user;
}

describe("reward redemption", () => {
  beforeEach(() => {
    signIn();
  });

  it("reuses the same idempotency key when a redemption is retried", async () => {
    const bodies: RedeemBody[] = [];
    let failFirst = true;
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        if (init?.method === "POST" && url === "/api/v1/rewards/redeem") {
          bodies.push(JSON.parse(String(init.body)) as RedeemBody);
          if (failFirst) {
            failFirst = false;
            throw new TypeError("Failed to fetch");
          }
          return Response.json(REDEEM_OK);
        }
        const payload = ROUTES[url];
        return payload === undefined
          ? Response.json({ error: `unmapped ${url}` }, { status: 404 })
          : Response.json(payload);
      }),
    );

    const user = await openWalletRedeem();
    await user.click(await screen.findByRole("button", { name: "تبدیل" }));

    // The network failure surfaces as a Persian message, never "Failed to fetch".
    expect(await screen.findByText(/اتصال به سرور برقرار نشد/)).toBeTruthy();
    expect(screen.queryByText(/Failed to fetch/)).toBeNull();

    await user.click(screen.getByRole("button", { name: "تبدیل" }));
    expect(
      await screen.findByText(/۳۰ دقیقه اعتبار به حساب شما اضافه شد/),
    ).toBeTruthy();

    expect(bodies).toHaveLength(2);
    expect(bodies[0].reward_code).toBe("credit_30");
    expect(bodies[0].idempotency_key).toBeTruthy();
    // The retry is the same logical request: same key, no double spend.
    expect(bodies[1].idempotency_key).toBe(bodies[0].idempotency_key);
  });

  it("never fires a second redemption while one is in flight", async () => {
    const bodies: RedeemBody[] = [];
    let release: ((value: Response) => void) | undefined;
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        if (init?.method === "POST" && url === "/api/v1/rewards/redeem") {
          bodies.push(JSON.parse(String(init.body)) as RedeemBody);
          return new Promise<Response>((resolve) => {
            release = resolve;
          });
        }
        const payload = ROUTES[url];
        return payload === undefined
          ? Response.json({ error: `unmapped ${url}` }, { status: 404 })
          : Response.json(payload);
      }),
    );

    const user = await openWalletRedeem();
    const button = await screen.findByRole("button", { name: "تبدیل" });
    await user.click(button);
    await user.click(button);
    await user.click(button);

    // The button is disabled in flight, so only one request exists.
    await waitFor(() => expect(bodies).toHaveLength(1));

    release?.(Response.json(REDEEM_OK));
    expect(
      await screen.findByText(/۳۰ دقیقه اعتبار به حساب شما اضافه شد/),
    ).toBeTruthy();
  });

  it("translates known API error strings and network failures to Persian", async () => {
    expect(describeError(new TypeError("Failed to fetch"))).toMatch(/اتصال به سرور/);
    expect(describeError(new ApiError("insufficient points", 400))).toMatch(/امتیاز شما/);
    expect(describeError(new ApiError("usage limit reached", 400))).toMatch(/سقف استفاده/);
    expect(describeError(new ApiError("boom", 500))).toMatch(/سرور گاماس/);
    expect(describeError(new ApiError("نشست", 401))).toMatch(/نشست تلگرام/);
  });
});
