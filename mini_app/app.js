/* Gamas Studio — Home / Leaderboard / Booklet Studio.
 *
 * No build step, no framework, no stored credentials. Telegram's signed
 * `initData` is attached to every request and verified by the WSGI app; the
 * browser is never trusted for an identity. Every button below performs a real
 * request: the ranking windows, the booklet editor, revision history, restore
 * and the DOCX export all have working server endpoints.
 */

(function () {
  "use strict";

  const api = {
    home: "/api/home",
    leaderboard: "/api/leaderboard",
    booklets: "/api/booklets",
    booklet: "/api/booklet",
    export: "/api/booklet/export",
    revisions: "/api/revisions",
    revision: "/api/revision",
  };

  const state = {
    initData: "",
    view: "home",
    window: "all",
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
    }, 2800);
  }

  async function call(url, body, method) {
    const response = await fetch(url, {
      method: method || "POST",
      headers: { "Content-Type": "application/json" },
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

  function renderHome(data) {
    const name = data.first_name || "";
    if (name) el("home-title").textContent = "خانهٔ " + name;
    el("home-credits").textContent = data.unlimited
      ? "نامحدود ⭐"
      : t(data.credits || 0) + " اعتبار";
    el("home-unlimited").hidden = !data.unlimited;
    el("home-points").textContent = t(data.quiz_points || 0);
    el("home-submissions").textContent = t(data.submission_count || 0);
    el("home-artifacts").textContent = t(data.artifact_count || 0);
    el("home-library").textContent = t(data.library_count || 0);
  }

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
      meta.textContent = item.chars ? t(item.chars) + " نویسه" : "متن ویرایش‌شده ندارد";
      body.append(title, meta);
      const open = document.createElement("button");
      open.textContent = "✏️ ویرایش";
      open.disabled = !item.editable;
      open.addEventListener("click", () => openEditor(item.id));
      row.append(body, open);
      list.append(row);
    });
  }

  function renderRevisions(items) {
    const list = el("revision-list");
    list.replaceChildren();
    el("revisions-empty").hidden = items.length > 0;
    items.forEach((item) => {
      const row = document.createElement("li");
      const meta = document.createElement("span");
      meta.textContent = t(item.created_at || "") + " · " + t(item.chars || 0) + " نویسه";
      const restore = document.createElement("button");
      restore.textContent = "↩️ بازگردانی";
      restore.addEventListener("click", () => restoreRevision(item.id));
      row.append(meta, restore);
      list.append(row);
    });
  }

  function show(view) {
    state.view = view;
    ["home", "leaderboard", "studio", "editor"].forEach((name) => {
      el("view-" + name).hidden = name !== view;
    });
    document.querySelectorAll(".tab").forEach((tab) => {
      const active = tab.dataset.view === (view === "editor" ? "studio" : view);
      tab.classList.toggle("is-active", active);
      tab.setAttribute("aria-selected", active ? "true" : "false");
    });
  }

  function confirmDiscard() {
    if (!state.dirty) return true;
    return window.confirm("تغییرات ذخیره نشده‌اند. بدون ذخیره برگردم؟");
  }

  /* ---------------------------------------------------------------- actions */

  async function loadHome() {
    try {
      renderHome(await call(api.home));
    } catch (error) {
      toast(error.message);
    }
  }

  async function loadRank() {
    try {
      renderRank(await call(api.leaderboard, { limit: 20, window: state.window }));
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

  async function loadRevisions() {
    if (!state.editing) return;
    try {
      const data = await call(api.revisions, { id: state.editing.id, limit: 20 });
      renderRevisions(data.items || []);
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
      el("editor-export").disabled = !data.editable;
      updateCount();
      show("editor");
      el("editor-text").focus();
      loadRevisions();
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
      loadRevisions();
      loadBooklets();
    } catch (error) {
      toast(error.message);
    } finally {
      button.disabled = false;
    }
  }

  async function exportDocx() {
    if (!state.editing) return;
    const button = el("editor-export");
    button.disabled = true;
    try {
      const response = await fetch(api.export, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ initData: state.initData, id: state.editing.id }),
        credentials: "omit",
        cache: "no-store",
      });
      if (!response.ok) {
        throw new Error(response.status === 401 ? "نشست تلگرام منقضی شده؛ برنامه را دوباره باز کنید." : "ساخت فایل Word ناموفق بود.");
      }
      const blob = await response.blob();
      const url = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = url;
      link.download = "gamas-booklet-" + state.editing.id + ".docx";
      document.body.append(link);
      link.click();
      link.remove();
      window.setTimeout(() => URL.revokeObjectURL(url), 1000);
      toast("فایل Word آماده شد ✅");
    } catch (error) {
      toast(error.message);
    } finally {
      button.disabled = false;
    }
  }

  async function restoreRevision(revisionId) {
    if (!state.editing) return;
    if (!window.confirm("این نسخه جایگزین متن فعلی شود؟ نسخهٔ فعلی در تاریخچه می‌ماند.")) return;
    try {
      await call(api.revision, { id: state.editing.id, revision_id: revisionId }, "PUT");
      const fresh = await call(api.booklet, { id: state.editing.id });
      el("editor-text").value = fresh.text || "";
      state.dirty = false;
      updateCount();
      toast("نسخه بازگردانی شد ✅");
      loadRevisions();
    } catch (error) {
      toast(error.message);
    }
  }

  function selectView(view) {
    if (!confirmDiscard()) return;
    show(view);
    if (view === "home") loadHome();
    else if (view === "leaderboard") loadRank();
    else loadBooklets();
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
      toast("این برنامه را از داخل تلگرام باز کنید.");
      return;
    }

    document.querySelectorAll(".tab").forEach((tab) => {
      tab.addEventListener("click", () => {
        if (tab.dataset.view !== state.view) selectView(tab.dataset.view);
      });
    });
    document.querySelectorAll(".segmented button").forEach((button) => {
      button.addEventListener("click", () => {
        state.window = button.dataset.window;
        document.querySelectorAll(".segmented button").forEach((other) => {
          other.classList.toggle("is-active", other === button);
        });
        loadRank();
      });
    });
    el("home-go-leaderboard").addEventListener("click", () => selectView("leaderboard"));
    el("home-go-studio").addEventListener("click", () => selectView("studio"));
    el("editor-back").addEventListener("click", () => selectView("studio"));
    el("editor-save").addEventListener("click", saveEditor);
    el("editor-export").addEventListener("click", exportDocx);
    el("editor-text").addEventListener("input", () => {
      state.dirty = true;
      updateCount();
    });
    window.addEventListener("beforeunload", (event) => {
      if (!state.dirty) return;
      event.preventDefault();
      event.returnValue = "";
    });

    loadHome();
    loadRank();
    loadBooklets();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
})();
