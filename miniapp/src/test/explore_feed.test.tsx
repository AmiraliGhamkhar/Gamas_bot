import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { ExploreTab } from "../features/explore/ExploreTab";

/**
 * The continuous feed contract.
 *
 * These tests pin the behaviour the redesign exists for: several questions on
 * one scroll surface, a cursor that advances, a prefetch that fires before the
 * end, no duplicated cards, a retry that keeps what is already on screen, a
 * source panel that never invents a citation, and an answer flow that needs
 * exactly one tap.
 */

const SOURCE = {
  source_type: "textbook",
  source_title: "Biology 2e",
  source_author: "OpenStax",
  source_institution: "Rice University",
  source_url: "https://openstax.org/details/books/biology-2e",
  source_year: "2018",
  source_page_or_section: "Chapters on cells",
  evidence_level: "textbook",
  verified: "true",
};

function card(id: number, text: string, verified = true) {
  return {
    question_id: id,
    topic: "زیست‌شناسی",
    difficulty: "easy",
    question_text: text,
    options: ["سلول", "بافت", "عضو", "دستگاه"],
    source: verified ? SOURCE : null,
    verified,
    explain: { affinity: 0.5, quality: 1, freshness: 0.9, discovery: 0.2, score: 0.7 },
  };
}

type Handler = (url: string, init?: RequestInit) => Response | Promise<Response>;

function installFetch(handler: Handler) {
  const calls: string[] = [];
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    calls.push(`${init?.method ?? "GET"} ${url}`);
    return handler(url, init);
  });
  vi.stubGlobal("fetch", fetchMock);
  return calls;
}

const PAGE_ONE = {
  items: [card(1, "پرسش نخست چیست؟"), card(2, "پرسش دوم چیست؟"), card(3, "پرسش سوم چیست؟")],
  next_cursor: "cursor-2",
  has_more: true,
};

const PAGE_TWO = {
  // Question 2 repeats on purpose: the client must not render it twice.
  items: [card(2, "پرسش دوم چیست؟"), card(4, "پرسش چهارم چیست؟")],
  next_cursor: "",
  has_more: false,
};

describe("Explore continuous feed", () => {
  beforeEach(() => {
    vi.unstubAllGlobals();
  });

  it("renders every card of the first page on one scroll surface", async () => {
    installFetch(() => Response.json(PAGE_ONE));
    render(<ExploreTab onReward={() => undefined} />);

    expect(await screen.findByText("پرسش نخست چیست؟")).toBeTruthy();
    expect(screen.getByText("پرسش دوم چیست؟")).toBeTruthy();
    expect(screen.getByText("پرسش سوم چیست؟")).toBeTruthy();
    // The feed is one scroll container with snap points, not one card at a time.
    const surface = screen.getByTestId("explore-feed");
    expect(surface.className).toContain("snap-y");
    expect(surface.className).toContain("overflow-y-auto");
    // No "next question" button drives navigation any more.
    expect(screen.queryByText("سؤال بعدی")).toBeNull();
  });

  it("advances the cursor and never duplicates a question", async () => {
    const calls = installFetch((url) =>
      url.includes("cursor=cursor-2") ? Response.json(PAGE_TWO) : Response.json(PAGE_ONE),
    );
    render(<ExploreTab onReward={() => undefined} />);
    await screen.findByText("پرسش نخست چیست؟");

    // jsdom has no IntersectionObserver; the manual "more" control is the
    // deterministic path to the second page in tests.
    await userEvent.click(screen.getByText("سؤال‌های بیشتر"));

    expect(await screen.findByText("پرسش چهارم چیست؟")).toBeTruthy();
    expect(screen.getAllByText("پرسش دوم چیست؟")).toHaveLength(1);
    expect(calls.some((call) => call.includes("cursor=cursor-2"))).toBe(true);
    expect(calls.every((call) => call.startsWith("GET /api/v1/explore/feed"))).toBe(true);
  });

  it("keeps loaded cards and offers a retry when the next page fails", async () => {
    let failNext = false;
    installFetch((url) => {
      if (url.includes("cursor=cursor-2") && failNext) {
        return Response.json({ error: "boom" }, { status: 500 });
      }
      return Response.json(PAGE_ONE);
    });
    render(<ExploreTab onReward={() => undefined} />);
    await screen.findByText("پرسش نخست چیست؟");

    failNext = true;
    await userEvent.click(screen.getByText("سؤال‌های بیشتر"));

    expect(await screen.findByText("بارگیری سؤال‌های بعدی انجام نشد.")).toBeTruthy();
    // Nothing already read is thrown away.
    expect(screen.getByText("پرسش نخست چیست؟")).toBeTruthy();
    expect(screen.getByText("تلاش دوباره")).toBeTruthy();
  });

  it("answers with one tap and shows the explanation, points and source", async () => {
    const onReward = vi.fn();
    const calls = installFetch((url, init) => {
      if (init?.method === "POST" && url === "/api/v1/explore/answers") {
        return Response.json({
          is_correct: true,
          correct_index: 0,
          explanation: "سلول واحد ساختاری بدن است.",
          points_awarded: 10,
          source: SOURCE,
          verified: true,
        });
      }
      if (url === "/api/v1/explore/engagement") return Response.json({ recorded: true });
      return Response.json(PAGE_ONE);
    });
    render(<ExploreTab onReward={onReward} />);
    await screen.findByText("پرسش نخست چیست؟");

    await userEvent.click(screen.getAllByText("سلول")[0]);

    expect(await screen.findByText("درست!")).toBeTruthy();
    expect(screen.getByText("+۱۰ امتیاز")).toBeTruthy();
    expect(screen.getByText("سلول واحد ساختاری بدن است.")).toBeTruthy();
    expect(onReward).toHaveBeenCalled();
    expect(calls.some((call) => call === "POST /api/v1/explore/answers")).toBe(true);

    // The citation is folded until asked for, then opens with its institution.
    await userEvent.click(within(screen.getAllByRole("article")[0]).getByText("منبع علمی"));
    expect((await screen.findAllByText(/Rice University/)).length).toBeGreaterThan(0);
    expect(screen.getByText("مشاهده منبع")).toBeTruthy();
  });

  it("marks an unverified question instead of pretending it is sourced", async () => {
    installFetch(() =>
      Response.json({
        items: [card(9, "پرسش بی‌منبع چیست؟", false)],
        next_cursor: "",
        has_more: false,
      }),
    );
    render(<ExploreTab onReward={() => undefined} />);

    expect(await screen.findByText("در انتظار راستی‌آزمایی")).toBeTruthy();
    expect(screen.queryByText("منبع علمی")).toBeNull();
    expect(screen.getByText(/منبع این سؤال هنوز راستی‌آزمایی نشده است/)).toBeTruthy();
  });

  it("offers a fresh session when the feed is exhausted", async () => {
    let round = 0;
    installFetch((url) => {
      if (!url.includes("cursor=")) round += 1;
      return Response.json(
        round === 1
          ? { items: [card(11, "تنها پرسش این دور؟")], next_cursor: "", has_more: false }
          : { items: [card(12, "پرسش دور تازه؟")], next_cursor: "", has_more: false },
      );
    });
    render(<ExploreTab onReward={() => undefined} />);
    await screen.findByText("تنها پرسش این دور؟");
    expect(await screen.findByText("سؤال تازه‌ای برای این دور نمانده است.")).toBeTruthy();

    await userEvent.click(screen.getByText("سؤال‌های تازه"));
    expect(await screen.findByText("پرسش دور تازه؟")).toBeTruthy();
  });

  it("shows the empty state when there is nothing to show", async () => {
    installFetch(() => Response.json({ items: [], next_cursor: "", has_more: false }));
    render(<ExploreTab onReward={() => undefined} />);
    expect(await screen.findByText("فعلاً سؤالی برای نمایش نیست")).toBeTruthy();
  });

  it("starts the load with a bounded first page", async () => {
    const calls = installFetch(() => Response.json(PAGE_ONE));
    render(<ExploreTab onReward={() => undefined} />);
    await screen.findByText("پرسش نخست چیست؟");
    await waitFor(() => expect(calls.length).toBeGreaterThan(0));
    // A feed must never ask for the whole bank at once.
    expect(calls[0]).toMatch(/\/api\/v1\/explore\/feed\?limit=\d+$/);
    const limit = Number(calls[0].match(/limit=(\d+)/)?.[1]);
    expect(limit).toBeGreaterThanOrEqual(10);
    expect(limit).toBeLessThanOrEqual(15);
  });
});
