import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import App from "../App";

/**
 * The smoke test walks the four tabs against the exact JSON shapes the backend
 * returns, so a contract drift between `api.ts` and the screens fails here
 * rather than inside somebody's Telegram client.
 */

const ME = {
  user_id: 4242,
  credit_seconds: 5400,
  reward_points: 120,
  free_uses: { quiz: false },
  banned: false,
  bot_username: "GamasBot",
};

const PLAN = {
  unlimited: false,
  credit_seconds: 5400,
  expires_at: "2026-12-01T10:00:00Z",
  plan: {
    code: "h5",
    hours: 5,
    price: 49000,
    original_price: 59000,
    validity_days: 30,
    purchased_at: "2026-11-20T10:00:00Z",
  },
};

const PLANS = {
  plans: [
    { code: "h5", hours: 5, price: 49000, original_price: 59000, validity_days: 30 },
    { code: "h10", hours: 10, price: 89000, original_price: null, validity_days: 30 },
  ],
};

const PACK = {
  pack_id: 7,
  user_id: 4242,
  title: "جزوهٔ فصل سوم زیست",
  source_type: "pdf",
  status: "done",
  created_at: "2026-11-21T08:00:00Z",
  updated_at: "2026-11-21T08:20:00Z",
  source_filename: "chapter3.pdf",
  asset_count: 2,
};

const ROUTES: Record<string, unknown> = {
  "/api/v1/me": ME,
  "/api/v1/me/plan": PLAN,
  "/api/v1/plans": PLANS,
  "/api/v1/me/referrals": { invited_count: 3 },
  "/api/v1/me/payments": {
    payments: [
      {
        payment_id: 31,
        plan_code: "h5",
        amount_toman: 49000,
        status: "approved",
        created_at: "2026-11-20T10:00:00Z",
        purchased_seconds: 18000,
      },
    ],
  },
  "/api/v1/me/subscription": { required_channel: null, channel_url: null, required: false },
  "/api/v1/rewards/catalog": {
    catalog: [{ code: "credit_30", title: "۳۰ دقیقه اعتبار", cost_points: 100, credit_seconds: 1800 }],
  },
  "/api/v1/library": { packs: [PACK] },
  "/api/v1/library/7": {
    pack: PACK,
    assets: [
      {
        asset_id: 11,
        pack_id: 7,
        kind: "booklet",
        title: "جزوهٔ نهایی",
        mime_type: "application/pdf",
        byte_size: 2048,
        created_at: "2026-11-21T08:20:00Z",
      },
    ],
    question_count: 4,
  },
  "/api/v1/review/groups?limit=50": {
    groups: [
      {
        group_key: "pack:7",
        pack_id: 7,
        title: "جزوهٔ فصل سوم زیست",
        source_type: "pdf",
        processing_status: "done",
        legacy: false,
        question_count: 4,
        answered_count: 1,
        correct_count: 1,
        latest_at: "2026-11-21T09:00:00Z",
      },
    ],
    total: 1,
  },
  "/api/v1/review/questions?group_key=pack%3A7&limit=100": {
    questions: [
      {
        question_id: 501,
        session_id: 9,
        group_key: "pack:7",
        pack_id: 7,
        pack_title: "جزوهٔ فصل سوم زیست",
        legacy: false,
        idx: 0,
        question: "کدام اندامک مسئول تولید انرژی است؟",
        options: ["هسته", "میتوکندری", "ریبوزوم", "گلژی"],
        answered: null,
        publication_status: null,
      },
    ],
    total: 1,
  },
  "/api/v1/explore/feed": {
    items: [
      {
        question_id: 900,
        topic: "biology",
        difficulty: "easy",
        question_text: "واحد ساختاری بدن چیست؟",
        options: ["سلول", "بافت", "عضو", "دستگاه"],
      },
    ],
  },
};

function installFetch() {
  const calls: string[] = [];
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    calls.push(`${init?.method ?? "GET"} ${url}`);
    if (init?.method === "POST") {
      if (url === "/api/v1/review/answers") {
        return Response.json({
          is_correct: true,
          correct_index: 1,
          explanation: "میتوکندری نیروگاه سلول است.",
          already_answered: false,
        });
      }
      if (url === "/api/v1/explore/answers") {
        return Response.json({
          is_correct: true,
          correct_index: 0,
          explanation: "سلول واحد ساختاری بدن است.",
          points_awarded: 10,
        });
      }
      if (url === "/api/v1/explore/engagement") return Response.json({ recorded: true });
      return Response.json({ ok: true });
    }
    const payload = ROUTES[url];
    if (payload === undefined) {
      return Response.json({ error: `unmapped ${url}` }, { status: 404 });
    }
    return Response.json(payload);
  });
  vi.stubGlobal("fetch", fetchMock);
  return calls;
}

function signIn() {
  (window as unknown as { Telegram: unknown }).Telegram = {
    WebApp: {
      initData: "auth_date=1&hash=stub",
      initDataUnsafe: { user: { first_name: "سارا", username: "sara" } },
    },
  };
}

const tab = (name: string) => screen.getByRole("tab", { name });

describe("Gamas Mini App shell", () => {
  beforeEach(() => {
    signIn();
  });

  it("opens on the account tab with the four required tabs", async () => {
    installFetch();
    render(<App />);

    expect(await screen.findByText("سارا")).toBeTruthy();
    expect(screen.getAllByRole("tab").map((node) => node.textContent)).toEqual([
      "حساب من",
      "کتابخانه",
      "مرور",
      "اکسپلور",
    ]);

    const account = screen.getByRole("tabpanel", { name: "حساب من" });
    expect(within(account).getByText("اعتبار گاماس")).toBeTruthy();
    // 5400s arrives from /me; the card splits it into a big "۱" and its unit.
    expect(within(account).getByText("ساعت و ۳۰ دقیقه")).toBeTruthy();
    // reward_points is shown in the stat row and again in the rewards card.
    expect(within(account).getAllByText("۱۲۰", { selector: ".nums" }).length).toBeGreaterThan(0);
    // Purchase options come from /plans; the price is never computed here.
    expect((await screen.findAllByText("۴۹٬۰۰۰ تومان")).length).toBeGreaterThan(0);
    expect(screen.getAllByText("۸۹٬۰۰۰ تومان").length).toBeGreaterThan(0);
    expect(await screen.findByText("تأییدشده")).toBeTruthy();
    expect(await screen.findByText("۳")).toBeTruthy();
  });

  it("lists study packs, opens a pack and reaches Review from it", async () => {
    const calls = installFetch();
    const user = userEvent.setup();
    render(<App />);
    await screen.findByText("سارا");

    await user.click(tab("کتابخانه"));
    const library = await screen.findByRole("tabpanel", { name: "کتابخانه" });
    const packTitle = await within(library).findByText("جزوهٔ فصل سوم زیست");
    expect(within(library).getByText("۲ فایل")).toBeTruthy();

    await user.click(packTitle);
    expect(await within(library).findByText("جزوهٔ نهایی")).toBeTruthy();
    expect(within(library).getByText("۲ کیلوبایت", { exact: false })).toBeTruthy();

    await user.click(within(library).getByText("مرور ۴ سؤال این بسته"));
    const review = await screen.findByRole("tabpanel", { name: "مرور" });
    expect(await within(review).findByText("کدام اندامک مسئول تولید انرژی است؟")).toBeTruthy();
    expect(calls.filter((call) => call === "GET /api/v1/library").length).toBe(1);
  });

  it("answers a review question and reports the server's verdict", async () => {
    installFetch();
    const user = userEvent.setup();
    render(<App />);
    await screen.findByText("سارا");

    await user.click(tab("مرور"));
    const review = await screen.findByRole("tabpanel", { name: "مرور" });
    await user.click(await within(review).findByText("جزوهٔ فصل سوم زیست"));

    await user.click(await within(review).findByText("میتوکندری"));
    expect(await within(review).findByText("پاسخ درست بود")).toBeTruthy();
    expect(within(review).getByText("میتوکندری نیروگاه سلول است.")).toBeTruthy();
  });

  it("answers an explore card and shows the awarded points from the server", async () => {
    const calls = installFetch();
    const user = userEvent.setup();
    render(<App />);
    await screen.findByText("سارا");

    await user.click(tab("اکسپلور"));
    const explore = await screen.findByRole("tabpanel", { name: "اکسپلور" });
    expect(await within(explore).findByText("واحد ساختاری بدن چیست؟")).toBeTruthy();
    // No internal counts or scores are exposed to the user.
    expect(within(explore).queryByText(/۲۰۰|امتیاز داخلی/)).toBeNull();

    await user.click(within(explore).getByText("سلول"));
    expect(await within(explore).findByText("درست!")).toBeTruthy();
    expect(within(explore).getByText("+۱۰ امتیاز")).toBeTruthy();

    // The reward refresh re-reads /me once; tabs never re-fetch on switching.
    await waitFor(() =>
      expect(calls.filter((call) => call === "GET /api/v1/me").length).toBeGreaterThan(1),
    );
    await user.click(tab("حساب من"));
    expect(calls.filter((call) => call === "GET /api/v1/library").length).toBe(0);
  });

  it("sends a plain browser to Telegram instead of asking for credentials", async () => {
    // No initData means the backend answers 401; the shell must not invent a login.
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => Response.json({ error: "invalid init data" }, { status: 401 })),
    );
    delete (window as unknown as { Telegram?: unknown }).Telegram;
    render(<App />);

    expect(await screen.findByText("گاماس داخل تلگرام باز می‌شود")).toBeTruthy();
    expect(screen.queryByText("تلاش مجدد")).toBeNull();
  });

  it("shows a retryable error instead of an empty screen when the API fails", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => Response.json({ error: "server exploded" }, { status: 500 })),
    );
    render(<App />);

    expect(await screen.findByText("گاماس باز نشد")).toBeTruthy();
    expect(screen.getByText("سرور گاماس پاسخ نداد؛ کمی بعد دوباره تلاش کنید.")).toBeTruthy();
    expect(screen.getByText("تلاش مجدد")).toBeTruthy();
  });
});
