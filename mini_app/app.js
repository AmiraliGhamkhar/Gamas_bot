/* Gamas Mini App — ranking and booklet editing.
 *
 * Design constraints that shaped this file:
 *
 * 1. **No build step.** The app is served by Passenger on a shared cPanel host;
 *    adding npm to that deployment is a bigger risk than adding a query string.
 *    So: plain ES2020, one file, no framework.
 * 2. **No stored credentials.** Telegram hands the app `initData`, which is
 *    signed and expires. It is sent with every request and never written to
 *    localStorage — a shared or lost phone must not carry a reusable token.
 * 3. **Nothing is computed locally.** The leaderboard and the booklet list come
 *    from SQLite; the app only renders them. That keeps the number the user sees
 *    identical to the number the bot shows.
 */

(function () {
  "use strict";

  const api = {
    rank: "/api/leaderboard",
    booklets: "/api/booklets",
    booklet: "/api/booklet",
  };

  const state = {
    initData: "",
    view: "rank",
    booklets: [],
    editing: null,
    dirty: false,
  };

  const el = (id) => document.getElementById(id);

  const t = (value) => String(value).replace(/\d/g, (d) => "۰۱۲۳۴۵۶۷۸۹"[Number(d)]);

  function toast(message) {
    const node = el("toast");
    node.textContent = message;
    node.hidden = false;
    window.clearTimeout(toast._timer);
    toast._timer = window.setTimeout(() => {
      node.hidden = true;
    }, 2600);
  }

  async function call(url, body, method) {
    const response = await fetch(url, {
      method: method || "POST",
      headers: { "Content-Type": "application/json" },
      // The session is re-proved on every call; nothing is cached.
      body: JSON.stringify(Object.assign({ initData: state.initData }, body || {})),
      credentials: "omit",
      cache: "no-store",
    });
    let payload = null;
    try {
      payload = await response.json();
    } catch (error) {
      payload = null;
    }
    if (!response.ok) {
      const message =
        response.status === 401
          ? "نشست تلگرام منقضی شده؛ برنامه را دوباره باز کنید."
          : (payload && payload.error) || "خطای سرور";
      const failure = new Error(message);
      failure.status = response.status;
      throw failure;
    }
    return payload || {};
  }

  /* ------------------------------------------------------------- rendering */

  function renderRank(data) {
    const list = el("rank-list");
    const empty = el("rank-empty");
    const meCard = el("me-card");
    list.replaceChildren();
    const rows = data.rows || [];
    empty.hidden = rows.length > 0;
    if (data.me && data.me.points > 0) {
      meCard.hidden = false;
      el("me-rank").textContent = "#" + t(data.me.rank);
      el("me-points").textContent = t(data.me.points) + " امتیاز";
    } else {
      meCard.hidden = true;
    }
    const medals = ["🥇", "🥈", "🥉"];
    rows.forEach((row) => {
      const item = document.createElement("li");
      if (data.me && row.rank === data.me.rank) item.classList.add("is-me");
      const medal = document.createElement("span");
      medal.className = "medal";
      medal.textContent = medals[row.rank - 1] || t(row.rank);
      const name = document.createElement("span");
      name.textContent = row.name;
      const points = document.createElement("span");
      points.className = "points";
      points.textContent = t(row.points);
      item.append(medal, name, points);
      list.append(item);
    });
  }

  function renderBooklets(items) {
    const list = el("booklet-list");
    list.replaceChildren();
    el("booklets-empty").hidden = items.length > 0;
    items.forEach((item) => {
      const row = document.createElement("li");
      const body = document.createElement("div");
      const title = document.createElement("span");
      title.className = "booklet-title";
      title.textContent = item.title || "جزوهٔ بی‌نام";
      const meta = document.createElement("span");
      meta.className = "booklet-meta";
      meta.textContent = item.chars
        ? t(item.chars) + " نویسه"
        : "متن ویرایش‌شده ندارد";
      body.append(title, meta);
      const open = document.createElement("button");
      open.textContent = "✏️ ویرایش";
      open.addEventListener("click", () => openEditor(item.id));
      row.append(body, open);
      list.append(row);
    });
  }

  function show(view) {
    state.view = view;
    ["rank", "booklets", "editor"].forEach((name) => {
      el("view-" + name).hidden = name !== view;
    });
    document.querySelectorAll(".tab").forEach((tab) => {
      const active = tab.dataset.view === (view === "editor" ? "booklets" : view);
      tab.classList.toggle("is-active", active);
      tab.setAttribute("aria-selected", active ? "true" : "false");
    });
  }

  function confirmDiscard() {
    if (!state.dirty) return true;
    return window.confirm("تغییرات ذخیره نشده‌اند. بدون ذخیره برگردم؟");
  }

  /* ---------------------------------------------------------------- actions */

  async function loadRank() {
    try {
      renderRank(await call(api.rank, { limit: 20 }));
    } catch (error) {
      toast(error.message);
    }
  }

  async function loadBooklets() {
    try {
      const data = await call(api.booklets, { limit: 50 });
      state.booklets = data.items || [];
      renderBooklets(state.booklets);
    } catch (error) {
      toast(error.message);
    }
  }

  async function openEditor(itemId) {
    if (!confirmDiscard()) return;
    try {
      const data = await call(api.booklet, { id: itemId });
      state.editing = data;
      state.dirty = false;
      el("editor-title").textContent = data.title || "ویرایش جزوه";
      el("editor-text").value = data.text || "";
      el("editor-text").readOnly = !data.editable;
      el("editor-save").disabled = !data.editable;
      updateCount();
      show("editor");
      el("editor-text").focus();
    } catch (error) {
      toast(error.message);
    }
  }

  function updateCount() {
    const length = el("editor-text").value.length;
    el("editor-count").textContent = t(length) + " نویسه";
  }

  async function saveEditor() {
    if (!state.editing) return;
    const text = el("editor-text").value.trim();
    if (!text) {
      toast("متن خالی را نمی‌توان ذخیره کرد.");
      return;
    }
    const button = el("editor-save");
    button.disabled = true;
    try {
      await call(api.booklet, { id: state.editing.id, text }, "PUT");
      state.dirty = false;
      toast("ذخیره شد ✅");
      await loadBooklets();
      show("booklets");
    } catch (error) {
      toast(error.message);
    } finally {
      button.disabled = false;
    }
  }

  /* ------------------------------------------------------------------ boot */

  function boot() {
    const webApp = window.Telegram && window.Telegram.WebApp;
    if (webApp) {
      webApp.ready();
      webApp.expand();
      try {
        webApp.setHeaderColor("secondary_bg_color");
      } catch (error) {
        /* older clients lack the method; the theme vars still apply */
      }
      state.initData = webApp.initData || "";
    }
    if (!state.initData) {
      // Opened outside Telegram (a bookmark, a preview): the API will refuse,
      // so say so plainly instead of showing three failed requests.
      toast("این برنامه را از داخل تلگرام باز کنید.");
      return;
    }

    document.querySelectorAll(".tab").forEach((tab) => {
      tab.addEventListener("click", () => {
        if (tab.dataset.view === state.view) return;
        if (!confirmDiscard()) return;
        show(tab.dataset.view);
        if (tab.dataset.view === "rank") loadRank();
        else loadBooklets();
      });
    });
    el("editor-back").addEventListener("click", () => {
      if (!confirmDiscard()) return;
      show("booklets");
      loadBooklets();
    });
    el("editor-save").addEventListener("click", saveEditor);
    el("editor-text").addEventListener("input", () => {
      state.dirty = true;
      updateCount();
    });
    window.addEventListener("beforeunload", (event) => {
      if (!state.dirty) return;
      event.preventDefault();
      event.returnValue = "";
    });

    loadRank();
    loadBooklets();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
})();
