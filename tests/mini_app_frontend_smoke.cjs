/* Lightweight Mini App boot tests using Node's built-in test/assert modules.
 * This intentionally does not emulate a browser layout engine or Telegram client.
 */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const source = fs.readFileSync(path.join(__dirname, "../mini_app/app.js"), "utf8");

class FakeElement {
  constructor(id) {
    this.id = id;
    this.hidden = false;
    this.textContent = "";
    this.dataset = {};
    this.attributes = {};
    this.listeners = {};
    this.children = [];
    this.style = { setProperty: (name, value) => { this.style[name] = value; } };
    this.classList = {
      values: new Set(),
      toggle: (name, enabled) => {
        if (enabled === undefined) enabled = !this.classList.values.has(name);
        if (enabled) this.classList.values.add(name);
        else this.classList.values.delete(name);
        return enabled;
      },
    };
  }
  addEventListener(name, callback) {
    (this.listeners[name] ||= []).push(callback);
  }
  setAttribute(name, value) { this.attributes[name] = String(value); }
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this.children = children; }
}

function setup({ telegram, fetchImpl, search = "" } = {}) {
  const elements = new Map();
  const get = (id) => {
    if (!elements.has(id)) elements.set(id, new FakeElement(id));
    return elements.get(id);
  };
  const tabs = ["home", "leaderboard", "studio"].map((view) => {
    const tab = new FakeElement("tab-" + view);
    tab.dataset.view = view;
    return tab;
  });
  const windows = ["all", "weekly", "monthly"].map((windowName) => {
    const button = new FakeElement("window-" + windowName);
    button.dataset.window = windowName;
    return button;
  });
  const timers = new Map();
  let nextTimer = 1;
  const windowListeners = {};
  const document = {
    readyState: "complete",
    documentElement: get("documentElement"),
    getElementById: get,
    createElement: (tag) => new FakeElement(tag),
    querySelectorAll(selector) {
      if (selector === ".tab") return tabs;
      if (selector === ".segmented button") return windows;
      return [];
    },
    addEventListener() {},
  };
  const window = {
    Telegram: telegram,
    location: { search },
    innerHeight: 780,
    performance: { now: () => Date.now() },
    setTimeout(callback, delay) {
      const id = nextTimer++;
      timers.set(id, { callback, delay });
      return id;
    },
    clearTimeout(id) { timers.delete(id); },
    addEventListener(name, callback) { (windowListeners[name] ||= []).push(callback); },
    scrollTo() {},
    confirm: () => true,
  };
  const requests = [];
  const fetcher = fetchImpl || (async (url, options) => {
    requests.push({ url, options });
    return {
      ok: true,
      status: 200,
      headers: { get: (name) => name.toLowerCase() === "x-request-id" ? "test-request" : null },
      json: async () => ({
        first_name: "آزمون", credits: 100, quiz_points: 12,
        submission_count: 2, artifact_count: 1, library_count: 3,
      }),
    };
  });
  const context = {
    window, document, fetch: fetcher, URLSearchParams, AbortController,
    Map, Set, JSON, Date, Math, String, Number, Object, Array, Error,
    console: { debug() {}, info() {}, warn() {}, error() {} },
  };
  vm.runInNewContext(source, context, { filename: "mini_app/app.js" });
  return { elements, tabs, windows, requests, timers, window, document, windowListeners };
}

function runTimerAt(env, delay) {
  for (const [id, timer] of [...env.timers]) {
    if (timer.delay === delay) {
      env.timers.delete(id);
      timer.callback();
    }
  }
}

async function flush() {
  await new Promise((resolve) => setImmediate(resolve));
  await new Promise((resolve) => setImmediate(resolve));
}

async function main() {
  // Telegram's SDK missing does not prevent a usable explanation/retry state.
  const missing = setup();
  runTimerAt(missing, 2200);
  assert.equal(missing.elements.get("init-alert").hidden, false);
  assert.match(missing.elements.get("init-message").textContent, /SDK تلگرام/);
  assert.equal(missing.elements.get("view-home").attributes["aria-busy"], "false");
  assert.equal(missing.requests.length, 0);

  // Startup acknowledges Telegram immediately, sends raw initData only to Home,
  // and defers both leaderboard and Studio until their views are opened.
  const callbacks = {};
  const back = { shown: 0, hidden: 0, show() { this.shown++; }, hide() { this.hidden++; }, onClick(fn) { this.click = fn; } };
  const telegram = {
    WebApp: {
      version: "8.0", platform: "android", colorScheme: "dark",
      themeParams: { bg_color: "#10131b", button_color: "#6655dd" },
      viewportHeight: 760, viewportStableHeight: 740,
      initData: "auth_date=123&user=%7B%22id%22%3A42%7D&hash=raw-signed-value",
      readyCalls: 0, ready() { this.readyCalls++; }, expand() {}, setHeaderColor() {},
      onEvent(name, fn) { callbacks[name] = fn; }, BackButton: back,
    },
  };
  const app = setup({ telegram });
  await flush();
  assert.equal(telegram.WebApp.readyCalls, 1);
  assert.equal(app.requests.length, 1);
  assert.equal(app.requests[0].url, "/api/home");
  const sent = JSON.parse(app.requests[0].options.body);
  assert.equal(sent.initData, telegram.WebApp.initData);
  assert.equal(app.elements.get("documentElement").dataset.theme, "dark");
  assert.equal(app.elements.get("view-home").attributes["aria-busy"], "false");
  assert.equal(back.hidden, 1);

  // Leaderboard and Studio each load on first entry, including empty states;
  // BackButton restores the previous view without another Home request.
  const rankTab = app.tabs.find((tab) => tab.dataset.view === "leaderboard");
  rankTab.listeners.click[0]();
  await flush();
  assert.equal(app.requests.length, 2);
  assert.equal(app.requests[1].url, "/api/leaderboard");
  assert.equal(app.elements.get("rank-empty").hidden, false);
  back.click();
  assert.equal(app.elements.get("view-home").hidden, false);
  assert.equal(app.requests.length, 2);
  const studioTab = app.tabs.find((tab) => tab.dataset.view === "studio");
  studioTab.listeners.click[0]();
  await flush();
  assert.equal(app.requests.length, 3);
  assert.equal(app.requests[2].url, "/api/submissions");
  assert.equal(app.elements.get("submissions-empty").hidden, false);
  assert.equal(back.shown, 2);
  back.click();
  assert.equal(app.elements.get("view-home").hidden, false);
  assert.equal(app.requests.length, 3);
  telegram.WebApp.colorScheme = "light";
  callbacks.themeChanged();
  assert.equal(app.elements.get("documentElement").dataset.theme, "light");
  telegram.WebApp.viewportStableHeight = 720;
  callbacks.viewportChanged();
  assert.equal(app.elements.get("documentElement").style["--tg-viewport-stable-height"], "720px");
  assert.equal(app.elements.get("documentElement").style["--tg-viewport-height"], "760px");

  // Read failure is persistent and manually retryable; no unsafe automatic
  // retries are introduced for any operation.
  let attempts = 0;
  const retryTelegram = { WebApp: { ...telegram.WebApp, ready() {}, onEvent() {}, BackButton: null } };
  const retry = setup({
    telegram: retryTelegram,
    fetchImpl: async (url) => {
      attempts++;
      if (attempts === 1) {
        return { ok: false, status: 503, headers: { get: () => "req-503" }, json: async () => ({ error: "temporary" }) };
      }
      return { ok: true, status: 200, headers: { get: () => "req-ok" }, json: async () => ({ credits: 4 }) };
    },
  });
  await flush();
  assert.equal(attempts, 1);
  assert.equal(retry.elements.get("home-retry").hidden, false);
  retry.elements.get("home-retry").listeners.click[0]();
  await flush();
  assert.equal(attempts, 2);
  assert.equal(retry.elements.get("home-retry").hidden, true);

  const slow = setup({
    telegram: { WebApp: { ...telegram.WebApp, ready() {}, onEvent() {}, BackButton: null } },
    fetchImpl: () => new Promise(() => {}),
  });
  const requestTimer = [...slow.timers].find(([, timer]) => timer.delay === 15000);
  assert.ok(requestTimer, "Home request has a bounded timeout");
  slow.timers.delete(requestTimer[0]);
  requestTimer[1].callback();
  await flush();
  assert.match(slow.elements.get("home-state-message").textContent, /پاسخی از سرور/);
  assert.equal(slow.elements.get("home-retry").hidden, false);

  const slowBody = setup({
    telegram: { WebApp: { ...telegram.WebApp, ready() {}, onEvent() {}, BackButton: null } },
    fetchImpl: async () => ({
      ok: true,
      status: 200,
      headers: { get: () => "req-slow-body" },
      json: () => new Promise(() => {}),
    }),
  });
  await flush();
  const bodyTimer = [...slowBody.timers].find(([, timer]) => timer.delay === 15000);
  assert.ok(bodyTimer, "response body parsing shares the request timeout");
  slowBody.timers.delete(bodyTimer[0]);
  bodyTimer[1].callback();
  await flush();
  assert.match(slowBody.elements.get("home-state-message").textContent, /پاسخی از سرور/);
  assert.equal(slowBody.elements.get("home-retry").hidden, false);

  // SDK presence without signed initData is never treated as authentication.
  const noAuth = setup({ telegram: { WebApp: { ready() {}, initData: "" } } });
  assert.equal(noAuth.elements.get("init-alert").hidden, false);
  assert.match(noAuth.elements.get("init-message").textContent, /منوی بات/);
  assert.equal(noAuth.requests.length, 0);

  console.log("Mini App frontend smoke tests passed (SDK failure, raw initData, lazy views, retry, theme, viewport, BackButton).");
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
