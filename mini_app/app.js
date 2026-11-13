/* Gamas Studio — Home / Leaderboard / Submissions / Speakers / Booklet editor.
 *
 * No build step, no framework, no stored credentials. Telegram's signed
 * `initData` is attached to every request and verified by the WSGI app; the
 * browser is never trusted for an identity. Every button below performs a real
 * request, and every rendered string goes through textContent — never
 * innerHTML — so booklet content can never inject markup.
 */

(function () {
  "use strict";

  const api = {
    home: "/api/home",
    leaderboard: "/api/leaderboard",
    submissions: "/api/submissions",
    booklets: "/api/booklets",
    booklet: "/api/booklet",
    export: "/api/booklet/export",
    revisions: "/api/revisions",
    revision: "/api/revision",
    speakers: "/api/speakers",
  };

  const state = {
    initData: "",
    webApp: null,
    webAppReady: false,
    booted: false,
    view: "home",
    history: [],
    window: "all",
    submissions: [],
    loaded: { home: false, leaderboard: {}, submissions: false },
    diagnostics: false,
    speakers: { submissionId: 0, data: null, dirty: false },
    editor: {
      itemId: 0,
      structured: false,
      doc: null,
      legacyText: "",
      legacyTitle: "",
      meta: {},
      dirty: false,
      preview: false,
      openSections: {},
    },
  };

  const inflightReads = new Map();
  const el = (id) => document.getElementById(id);
  const t = (value) => String(value).replace(/\d/g, (d) => "۰۱۲۳۴۵۶۷۸۹"[Number(d)]);

  function diagnostic(message) {
    if (!state.diagnostics) return;
    const list = el("diagnostics-list");
    if (!list) return;
    list.append(h("li", { text: message }));
  }

  function updateInitAlert(title, message, visible) {
    const alert = el("init-alert");
    if (!alert) return;
    el("init-title").textContent = title;
    el("init-message").textContent = message;
    alert.hidden = !visible;
  }

  const STATUS_LABELS = {
    pending: ["در صف", "wait"],
    processing: ["در حال پردازش", "busy"],
    done: ["کامل", "ok"],
    partial: ["ناقص", "warn"],
    failed: ["ناموفق", "bad"],
    rejected: ["ردشده", "bad"],
  };

  const KIND_LABELS = {
    audio: "صوت",
    video: "ویدیو",
    document: "سند",
    presentation: "ارائه",
    image: "تصویر",
  };

  /* ------------------------------------------------------------- helpers */

  function toast(message) {
    const node = el("toast");
    node.textContent = message;
    node.hidden = false;
    window.clearTimeout(toast._timer);
    toast._timer = window.setTimeout(() => {
      node.hidden = true;
    }, 3200);
  }

  async function call(url, body, method, options) {
    const settings = options || {};
    const requestMethod = method || "POST";
    const requestBody = Object.assign({ initData: state.initData }, body || {});
    const readKey = url + "|" + JSON.stringify(body || {});
    if (settings.readOnly && inflightReads.has(readKey)) {
      return inflightReads.get(readKey);
    }

    const task = (async () => {
      const controller = typeof AbortController === "function" ? new AbortController() : null;
      const started = window.performance && window.performance.now ? window.performance.now() : Date.now();
      let timedOut = false;
      let timer;
      const timeout = new Promise((resolve, reject) => {
        timer = window.setTimeout(() => {
          timedOut = true;
          if (controller) controller.abort();
          reject(new Error("request timed out"));
        }, settings.timeout || 15000);
      });
      try {
        const operation = (async () => {
          const response = await fetch(url, {
            method: requestMethod,
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(requestBody),
            credentials: "omit",
            cache: "no-store",
            signal: controller ? controller.signal : undefined,
          });
          const requestId = response.headers.get("X-Request-ID") || "";
          diagnostic(
            "API " + url + " → " + response.status + " · " +
              Math.round((window.performance && window.performance.now ? window.performance.now() : Date.now()) - started) + "ms" +
              (requestId ? " · " + requestId : "")
          );
          const payload = await response.json();
          if (!response.ok) {
            const message = response.status === 401
              ? "نشست تلگرام منقضی شده؛ برنامه را دوباره باز کنید."
              : (payload && payload.error) || "خطای سرور";
            const failure = new Error(message);
            failure.status = response.status;
            failure.requestId = requestId;
            throw failure;
          }
          return payload || {};
        })();
        return await Promise.race([operation, timeout]);
      } catch (error) {
        if (error && error.status) throw error;
        const failure = new Error(
          timedOut
            ? "پاسخی از سرور دریافت نشد. اتصال را بررسی کنید و دوباره تلاش کنید."
            : "ارتباط با سرور برقرار نشد. اینترنت خود را بررسی کنید و دوباره تلاش کنید."
        );
        failure.network = true;
        failure.timeout = timedOut;
        throw failure;
      } finally {
        window.clearTimeout(timer);
      }
    })();

    if (settings.readOnly) {
      inflightReads.set(readKey, task);
      task.then(
        () => inflightReads.delete(readKey),
        () => inflightReads.delete(readKey)
      );
    }
    return task;
  }

  async function callBinary(url, body, timeoutMs) {
    const controller = typeof AbortController === "function" ? new AbortController() : null;
    let timedOut = false;
    const started = window.performance && window.performance.now ? window.performance.now() : Date.now();
    let timer;
    const timeout = new Promise((resolve, reject) => {
      timer = window.setTimeout(() => {
        timedOut = true;
        if (controller) controller.abort();
        reject(new Error("file request timed out"));
      }, timeoutMs || 60000);
    });
    try {
      const operation = (async () => {
        const response = await fetch(url, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(Object.assign({ initData: state.initData }, body || {})),
          credentials: "omit",
          cache: "no-store",
          signal: controller ? controller.signal : undefined,
        });
        const requestId = response.headers.get("X-Request-ID") || "";
        diagnostic(
          "API " + url + " → " + response.status + " · " +
            Math.round((window.performance && window.performance.now ? window.performance.now() : Date.now()) - started) + "ms" +
            (requestId ? " · " + requestId : "")
        );
        if (!response.ok) {
          let payload = null;
          try { payload = await response.json(); } catch (error) { payload = null; }
          const failure = new Error(
            response.status === 401
              ? "نشست تلگرام منقضی شده؛ برنامه را دوباره باز کنید."
              : (payload && payload.error) || "ساخت فایل ناموفق بود."
          );
          failure.status = response.status;
          failure.requestId = requestId;
          throw failure;
        }
        return await response.blob();
      })();
      return await Promise.race([operation, timeout]);
    } catch (error) {
      if (error && error.status) throw error;
      const failure = new Error(
        timedOut
          ? "پاسخی برای دریافت فایل نرسید؛ دوباره تلاش کنید."
          : "ارتباط برای دریافت فایل برقرار نشد؛ اینترنت را بررسی کنید."
      );
      failure.timeout = timedOut;
      throw failure;
    } finally {
      window.clearTimeout(timer);
    }
  }

  function setLoadState(prefix, message, error) {
    const box = el(prefix + "-state");
    if (!box) return;
    el(prefix + "-state-message").textContent = message;
    const retry = el(prefix + "-retry");
    if (retry) retry.hidden = !error;
    box.classList.toggle("has-error", Boolean(error));
    box.hidden = !message;
    const view = el("view-" + prefix);
    if (view) view.setAttribute("aria-busy", message && !error ? "true" : "false");
  }

  function faDate(iso) {
    if (!iso) return "";
    try {
      return new Date(iso).toLocaleDateString("fa-IR", {
        year: "numeric",
        month: "long",
        day: "numeric",
      });
    } catch (error) {
      return String(iso).slice(0, 10);
    }
  }

  function faDuration(seconds) {
    if (!seconds || seconds <= 0) return "";
    const minutes = Math.round(seconds / 60);
    if (minutes < 60) return t(minutes) + " دقیقه";
    const hours = Math.floor(minutes / 60);
    const rest = minutes % 60;
    return t(hours) + " ساعت" + (rest ? " و " + t(rest) + " دقیقه" : "");
  }

  function faClock(seconds) {
    if (seconds === null || seconds === undefined || seconds < 0) return "";
    const total = Math.floor(Number(seconds) || 0);
    const minutes = Math.floor(total / 60);
    const rest = total % 60;
    return t(String(minutes).padStart(2, "0") + ":" + String(rest).padStart(2, "0"));
  }

  /* Create an element with attributes and children in one call. */
  function h(tag, attrs) {
    const node = document.createElement(tag);
    if (attrs) {
      for (const key of Object.keys(attrs)) {
        const value = attrs[key];
        if (value === null || value === undefined || value === false) continue;
        if (key === "class") node.className = value;
        else if (key === "text") node.textContent = value;
        else if (key === "dataset") Object.assign(node.dataset, value);
        else node.setAttribute(key, value);
      }
    }
    for (let i = 2; i < arguments.length; i += 1) {
      const child = arguments[i];
      if (child === null || child === undefined || child === false) continue;
      node.append(child);
    }
    return node;
  }

  function statusChip(status) {
    const pair = STATUS_LABELS[status] || [status || "نامشخص", "wait"];
    return h("span", { class: "chip chip-" + pair[1], text: pair[0] });
  }

  function syncBackButton() {
    const backButton = state.webApp && state.webApp.BackButton;
    if (!backButton) return;
    try {
      if (state.view === "home" || !state.history.length) backButton.hide();
      else backButton.show();
    } catch (error) {
      diagnostic("Telegram BackButton unavailable");
    }
  }

  function show(view, remember) {
    if (view !== state.view && remember !== false) state.history.push(state.view);
    state.view = view;
    ["home", "leaderboard", "studio", "speakers", "editor"].forEach((name) => {
      const node = el("view-" + name);
      if (node) node.hidden = name !== view;
    });
    document.querySelectorAll(".tab").forEach((tab) => {
      const active =
        tab.dataset.view === view ||
        (tab.dataset.view === "studio" && (view === "speakers" || view === "editor"));
      tab.classList.toggle("is-active", active);
      tab.setAttribute("aria-selected", active ? "true" : "false");
    });
    syncBackButton();
    window.scrollTo({ top: 0, behavior: "auto" });
  }

  function applyTelegramTheme() {
    const webApp = state.webApp;
    const root = document.documentElement;
    const scheme = webApp && webApp.colorScheme;
    root.dataset.theme = scheme === "dark" ? "dark" : "light";
    const theme = webApp && webApp.themeParams;
    const themeVariables = {
      bg_color: "--tg-theme-bg-color",
      secondary_bg_color: "--tg-theme-secondary-bg-color",
      text_color: "--tg-theme-text-color",
      hint_color: "--tg-theme-hint-color",
      button_color: "--tg-theme-button-color",
      button_text_color: "--tg-theme-button-text-color",
      link_color: "--tg-theme-link-color",
      destructive_text_color: "--tg-theme-destructive-text-color",
    };
    if (theme) {
      Object.keys(themeVariables).forEach((name) => {
        const value = theme[name];
        if (typeof value === "string" && /^#[0-9a-fA-F]{3,8}$/.test(value)) {
          root.style.setProperty(themeVariables[name], value);
        }
      });
    }
    applyTelegramViewport();
  }

  function applyTelegramViewport() {
    const webApp = state.webApp;
    const stable = Number(webApp && webApp.viewportStableHeight) || 0;
    const current = Number(webApp && webApp.viewportHeight) || 0;
    if (current > 0) {
      document.documentElement.style.setProperty("--tg-viewport-height", current + "px");
    }
    if (stable > 0) {
      document.documentElement.style.setProperty("--tg-viewport-stable-height", stable + "px");
    }
    diagnostic(
      "Viewport updated · current " + Math.round(current || window.innerHeight || 0) +
        "px / stable " + Math.round(stable || current || window.innerHeight || 0) + "px"
    );
  }

  function confirmDiscard() {
    if (!state.editor.dirty && !state.speakers.dirty) return true;
    return window.confirm("تغییرات ذخیره نشده‌اند. بدون ذخیره ادامه بدهم؟");
  }

  /* ---------------------------------------------------------- rendering */

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
      const item = h("li", { class: data.me && row.rank === data.me.rank ? "is-me" : null });
      item.append(
        h("span", { class: "medal", text: medals[row.rank - 1] || t(row.rank) }),
        h("span", { text: row.name }),
        h("span", { class: "points", text: t(row.points) })
      );
      list.append(item);
    });
  }

  function renderSubmissions(items) {
    const list = el("submission-list");
    list.replaceChildren();
    el("submissions-empty").hidden = items.length > 0;
    items.forEach((item) => {
      const actions = h("div", { class: "submission-actions" });
      if (item.booklet_item_id) {
        actions.append(
          h("button", {
            class: "primary",
            text: "✏️ ویرایش جزوه",
            dataset: { role: "edit", id: String(item.booklet_item_id) },
          })
        );
        actions.append(
          h("button", {
            class: "secondary",
            text: "👁 پیش‌نمایش",
            dataset: { role: "preview", id: String(item.booklet_item_id) },
          })
        );
      }
      if (item.has_speakers) {
        actions.append(
          h("button", {
            class: "secondary",
            text: "👥 گوینده‌ها",
            dataset: { role: "speakers", id: String(item.id) },
          })
        );
      }
      if (item.booklet_item_id && (item.has_pdf || item.has_docx)) {
        actions.append(
          h("button", {
            class: "secondary",
            text: "⬇️ خروجی‌ها",
            dataset: { role: "exports", id: String(item.booklet_item_id), pdf: item.has_pdf ? "1" : "", docx: item.has_docx ? "1" : "" },
          })
        );
      }
      const meta = [
        KIND_LABELS[item.kind] || item.kind || "",
        faDate(item.created_at),
        faDuration(item.duration_seconds),
      ].filter(Boolean);
      const row = h(
        "li",
        { class: "card submission" },
        h(
          "div",
          { class: "submission-head" },
          h("strong", { class: "submission-title", text: item.title || "گفتار بی‌نام" }),
          statusChip(item.status)
        ),
        h("span", { class: "booklet-meta", text: meta.join(" · ") }),
        item.filename && item.filename !== item.title
          ? h("span", { class: "booklet-meta muted", text: "فایل: " + item.filename })
          : null,
        h(
          "div",
          { class: "submission-docs" },
          item.has_pdf ? h("span", { class: "chip chip-ok", text: "📘 جزوه PDF" }) : null,
          item.has_docx ? h("span", { class: "chip chip-ok", text: "📄 نسخه Word" }) : null,
          item.has_speakers ? h("span", { class: "chip chip-ok", text: "👥 گوینده‌ها" }) : null
        ),
        actions
      );
      list.append(row);
    });
  }

  /* ----------------------------------------------------------- speakers */

  function speakerTurnsOf(label) {
    const data = state.speakers.data || { turns: [] };
    return (data.turns || []).filter((turn) =>
      label === null
        ? !turn.speaker_label
        : String(turn.speaker_label) === String(label)
    );
  }

  function renderSpeakers() {
    const data = state.speakers.data;
    const list = el("speaker-list");
    list.replaceChildren();
    const summary = el("speakers-summary");
    summary.replaceChildren();
    summary.hidden = true;
    if (!data) return;
    const speakers = data.speakers || [];
    el("speakers-empty").hidden = speakers.length > 0 || !data.unknown_attribution;
    if (speakers.length || data.unknown_attribution) {
      summary.hidden = false;
      summary.append(
        h("strong", { text: "گویندگان شناسایی‌شده: " + t(speakers.length) + " نفر" }),
        h(
          "span",
          {
            class: "hint",
            text:
              "نام پیش‌فرض فقط برچسب است. اگر مطمئن نیستید، نام پیش‌فرض را نگه دارید." +
              (data.unknown_attribution ? " بخشی از گفتار بدون تشخیص گوینده است." : ""),
          }
        )
      );
    }
    speakers.forEach((speaker) => {
      const turns = speakerTurnsOf(speaker.label);
      const nameInput = h("input", {
        type: "text",
        class: "field-input",
        value: speaker.custom_name || "",
        placeholder: speaker.default_name || "گویندهٔ " + t(Number(speaker.label) + 1),
        maxlength: "64",
        "aria-label": "نام دلخواه برای " + (speaker.default_name || speaker.label),
        dataset: { role: "speaker-name", label: String(speaker.label) },
      });
      const roleInput = h("input", {
        type: "text",
        class: "field-input",
        value: speaker.role || "",
        placeholder: "نقش یا توضیح (اختیاری)",
        maxlength: "64",
        "aria-label": "نقش اختیاری برای " + (speaker.default_name || speaker.label),
        dataset: { role: "speaker-role", label: String(speaker.label) },
      });
      const details = h("details", { class: "segments" });
      const summaryNode = h("summary", {
        text: "گفتارهای این گوینده (" + t(speaker.segments) + " قطعه)",
      });
      const turnList = h("ul", { class: "turn-list" });
      turns.forEach((turn) => {
        turnList.append(
          h(
            "li",
            { class: turn.overlaps ? "turn overlap" : "turn" },
            h("span", { class: "turn-time", text: faClock(turn.start) }),
            h("span", { class: "turn-text", text: turn.text || "…" })
          )
        );
      });
      details.append(summaryNode, turnList);
      const timeRange =
        speaker.first_start !== null && speaker.last_end !== null
          ? faClock(speaker.first_start) + " تا " + faClock(speaker.last_end)
          : "";
      const card = h(
        "li",
        { class: "card speaker" },
        h(
          "div",
          { class: "speaker-head" },
          h("strong", { text: speaker.default_name || "گویندهٔ " + t(Number(speaker.label) + 1) }),
          h("span", { class: "booklet-meta", text: timeRange })
        ),
        h("label", { class: "field" }, h("span", { text: "نام نمایشی (خودتان بنویسید)" }), nameInput),
        h("label", { class: "field" }, h("span", { text: "نقش یا توضیح (اختیاری)" }), roleInput),
        details
      );
      list.append(card);
    });
    if (data.unknown_attribution) {
      const turns = speakerTurnsOf(null);
      const details = h("details", { class: "segments" });
      const turnList = h("ul", { class: "turn-list" });
      turns.forEach((turn) => {
        turnList.append(
          h(
            "li",
            { class: turn.overlaps ? "turn overlap" : "turn" },
            h("span", { class: "turn-time", text: faClock(turn.start) }),
            h("span", { class: "turn-text", text: turn.text || "…" })
          )
        );
      });
      details.append(
        h("summary", { text: "گفتار بدون تشخیص گوینده (" + t(turns.length) + " قطعه)" }),
        turnList
      );
      list.append(
        h("li", { class: "card speaker" }, h("strong", { text: "گویندهٔ ناشناس" }), details)
      );
    }
  }

  async function openSpeakers(submissionId) {
    state.speakers = { submissionId: Number(submissionId), data: null, dirty: false };
    el("speaker-list").replaceChildren();
    el("turns-view").replaceChildren();
    el("speakers-summary").hidden = true;
    el("speakers-save").disabled = true;
    setLoadState("speakers", "در حال دریافت گویندگان و متن گفتار…", false);
    show("speakers");
    try {
      const data = await call(
        api.speakers,
        { submission_id: Number(submissionId) },
        "POST",
        { readOnly: true, timeout: 30000 }
      );
      state.speakers.data = data;
      renderSpeakers();
      el("speakers-save").disabled = false;
      setLoadState("speakers", "", false);
    } catch (error) {
      setLoadState("speakers", error.message || "دریافت گویندگان ناموفق بود.", true);
      diagnostic("Speaker request failed" + (error.status ? " · HTTP " + error.status : ""));
    }
  }

  async function saveSpeakers() {
    const data = state.speakers.data;
    if (!data || !state.speakers.submissionId) return;
    const button = el("speakers-save");
    button.disabled = true;
    try {
      let saved = 0;
      for (const speaker of data.speakers || []) {
        const nameNode = document.querySelector(
          '[data-role="speaker-name"][data-label="' + speaker.label + '"]'
        );
        const roleNode = document.querySelector(
          '[data-role="speaker-role"][data-label="' + speaker.label + '"]'
        );
        const name = (nameNode ? nameNode.value : "").trim();
        const role = (roleNode ? roleNode.value : "").trim();
        const nextName = name || speaker.custom_name || "";
        if (!nextName && !(speaker.custom_name || "")) continue; // nothing to save for this one
        if (nextName === (speaker.custom_name || "") && role === (speaker.role || "")) continue;
        await call(
          api.speakers,
          {
            submission_id: state.speakers.submissionId,
            label: String(speaker.label),
            name: nextName,
            role: role,
          },
          "PUT"
        );
        saved += 1;
      }
      state.speakers.dirty = false;
      el("speakers-dirty").hidden = true;
      if (saved > 0) {
        toast("نام گوینده‌ها ذخیره شد ✅");
        const fresh = await call(
          api.speakers,
          { submission_id: state.speakers.submissionId },
          "POST",
          { readOnly: true }
        );
        state.speakers.data = fresh;
        renderSpeakers();
      } else {
        toast("تغییری برای ذخیره نبود.");
      }
    } catch (error) {
      toast(error.message);
    } finally {
      button.disabled = false;
    }
  }

  /* -------------------------------------------------------- book editing */

  const BLOCK_TYPES = {
    paragraph: "پاراگراف",
    definition: "تعریف",
    bullet: "مورد فهرست",
    example: "مثال",
    step: "مرحله",
    formula: "فرمول",
    key_point: "نکتهٔ کلیدی",
    callout: "کادر تأکید",
    table: "جدول",
  };

  const CALLOUT_KINDS = ["نکته", "هشدار", "یادآوری"];

  function sectionBlocks(section) {
    const blocks = [];
    (section.paragraphs || []).forEach((text) =>
      blocks.push({ type: "paragraph", text: text })
    );
    (section.definitions || []).forEach((def) =>
      blocks.push({
        type: "definition",
        term: def.term || "",
        term_en: def.term_en || "",
        definition: def.definition || "",
      })
    );
    (section.bullets || []).forEach((text) => blocks.push({ type: "bullet", text: text }));
    (section.examples || []).forEach((text) => blocks.push({ type: "example", text: text }));
    (section.steps || []).forEach((text) => blocks.push({ type: "step", text: text }));
    (section.formulas || []).forEach((text) => blocks.push({ type: "formula", text: text }));
    (section.key_points || []).forEach((text) => blocks.push({ type: "key_point", text: text }));
    (section.callouts || []).forEach((callout) =>
      blocks.push({
        type: "callout",
        kind: CALLOUT_KINDS.indexOf(callout.kind) >= 0 ? callout.kind : "نکته",
        text: callout.text || "",
      })
    );
    (section.tables || []).forEach((table) =>
      blocks.push({
        type: "table",
        caption: table.caption || "",
        headers: (table.headers || []).map((cell) => String(cell)),
        rows: (table.rows || []).map((row) => row.map((cell) => String(cell))),
      })
    );
    return blocks;
  }

  function assembleSection(section) {
    const out = {
      heading: section.heading || "",
      paragraphs: [],
      definitions: [],
      bullets: [],
      examples: [],
      steps: [],
      formulas: [],
      key_points: [],
      callouts: [],
      tables: [],
    };
    (section.blocks || []).forEach((block) => {
      const text = (block.text || "").trim();
      switch (block.type) {
        case "paragraph":
          if (text) out.paragraphs.push(block.text);
          break;
        case "definition":
          if ((block.term || "").trim() && (block.definition || "").trim()) {
            out.definitions.push({
              term: block.term,
              term_en: block.term_en || "",
              definition: block.definition,
            });
          }
          break;
        case "bullet":
          if (text) out.bullets.push(block.text);
          break;
        case "example":
          if (text) out.examples.push(block.text);
          break;
        case "step":
          if (text) out.steps.push(block.text);
          break;
        case "formula":
          if (text) out.formulas.push(block.text);
          break;
        case "key_point":
          if (text) out.key_points.push(block.text);
          break;
        case "callout":
          if (text) {
            out.callouts.push({
              kind: CALLOUT_KINDS.indexOf(block.kind) >= 0 ? block.kind : "نکته",
              text: block.text,
            });
          }
          break;
        case "table": {
          const headers = (block.headers || []).map((cell) => String(cell || ""));
          const rows = (block.rows || [])
            .map((row) => row.map((cell) => String(cell || "")))
            .filter((row) => row.some((cell) => cell.trim()));
          if (headers.some((cell) => cell.trim()) && rows.length) {
            out.tables.push({ caption: block.caption || "", headers: headers, rows: rows });
          }
          break;
        }
        default:
          break;
      }
    });
    return out;
  }

  function assembleDocument() {
    const doc = state.editor.doc;
    return {
      title: doc.title || "",
      summary: doc.summary || "",
      learning_objectives: (doc.learning_objectives || []).filter((x) => x && x.trim()),
      review_questions: (doc.review_questions || []).filter((x) => x && x.trim()),
      key_points: (doc.key_points || []).filter((x) => x && x.trim()),
      sections: (doc.sections || []).map(assembleSection),
      glossary: (doc.glossary || []).filter((entry) => (entry.term || "").trim() && (entry.definition || "").trim()),
      practical_tips: (doc.practical_tips || [])
        .filter((tip) => (tip.text || "").trim())
        .map((tip) => ({ text: tip.text, kind: tip.kind || "کاربردی", basis: tip.basis || "" })),
      speakers: doc.speakers || [],
      timeline: doc.timeline || [],
    };
  }

  function hasEditableContent(doc) {
    if (!doc) return false;
    if ((doc.summary || "").trim()) return true;
    if ((doc.sections || []).some((section) => section.blocks.length)) return true;
    if ((doc.key_points || []).length) return true;
    if ((doc.glossary || []).length) return true;
    if ((doc.learning_objectives || []).length) return true;
    if ((doc.review_questions || []).length) return true;
    return false;
  }

  /* ---------------------------------------------------------------- editor */

  function markDirty() {
    if (state.view === "speakers") {
      state.speakers.dirty = true;
      el("speakers-dirty").hidden = false;
      return;
    }
    state.editor.dirty = true;
    el("editor-dirty").hidden = false;
  }

  function moveItem(list, index, delta) {
    const target = index + delta;
    if (target < 0 || target >= list.length) return;
    const item = list.splice(index, 1)[0];
    list.splice(target, 0, item);
    markDirty();
    renderEditor();
  }

  function removeItem(list, index) {
    list.splice(index, 1);
    markDirty();
    renderEditor();
  }

  function convertBlock(block, nextType) {
    if (nextType === block.type) return;
    const text =
      block.type === "definition"
        ? [block.term, block.definition].filter(Boolean).join(": ")
        : block.type === "callout"
          ? block.text
          : block.type === "table"
            ? block.caption || ""
            : block.text || "";
    if (nextType === "definition") {
      block.term = (text || "").split(":")[0].slice(0, 120);
      block.term_en = "";
      block.definition = text || "";
    } else if (nextType === "table") {
      block.caption = text || "";
      block.headers = ["ستون ۱", "ستون ۲"];
      block.rows = [["", ""], ["", ""]];
    }
    block.type = nextType;
    if (nextType !== "definition" && nextType !== "table") {
      block.text = text;
      if (nextType === "callout" && !block.kind) block.kind = "نکته";
      delete block.term;
      delete block.term_en;
      delete block.definition;
      delete block.headers;
      delete block.rows;
      delete block.caption;
    }
    if (nextType === "definition") {
      delete block.text;
      delete block.kind;
    }
    if (nextType === "table") {
      delete block.text;
      delete block.kind;
    }
    markDirty();
    renderEditor();
  }

  function blockToolbar(list, index, block) {
    const bar = h("div", { class: "block-tools" });
    const typeSelect = h(
      "select",
      { class: "type-select", "aria-label": "نوع بلوک", dataset: { role: "block-type", index: String(index) } },
      ...Object.keys(BLOCK_TYPES).map((type) =>
        h("option", { value: type, text: BLOCK_TYPES[type], selected: type === block.type ? "" : null })
      )
    );
    bar.append(typeSelect);
    bar.append(
      h("button", { class: "icon", text: "▲", title: "انتقال به بالا", dataset: { role: "block-up", index: String(index) } }),
      h("button", { class: "icon", text: "▼", title: "انتقال به پایین", dataset: { role: "block-down", index: String(index) } }),
      h("button", { class: "icon danger", text: "✕", title: "حذف بلوک", dataset: { role: "block-del", index: String(index) } })
    );
    return bar;
  }

  function blockBody(block, index) {
    const body = h("div", { class: "block-body" });
    const onInput = () => markDirty();
    if (block.type === "paragraph" || block.type === "bullet" || block.type === "example" || block.type === "step" || block.type === "formula" || block.type === "key_point") {
      const area = h("textarea", {
        class: "field-input",
        rows: block.type === "paragraph" ? "3" : "2",
        dir: "auto",
      });
      area.value = block.text || "";
      area.addEventListener("input", () => {
        block.text = area.value;
        onInput();
      });
      body.append(area);
      return body;
    }
    if (block.type === "definition") {
      const grid = h("div", { class: "def-grid" });
      const term = h("input", { type: "text", class: "field-input", placeholder: "اصطلاح", dataset: { role: "def-term", index: String(index) } });
      term.value = block.term || "";
      term.addEventListener("input", () => {
        block.term = term.value;
        onInput();
      });
      const termEn = h("input", { type: "text", class: "field-input", dir: "ltr", placeholder: "English (اختیاری)", dataset: { role: "def-en", index: String(index) } });
      termEn.value = block.term_en || "";
      termEn.addEventListener("input", () => {
        block.term_en = termEn.value;
        onInput();
      });
      const definition = h("textarea", { class: "field-input", rows: "2", placeholder: "تعریف", dataset: { role: "def-text", index: String(index) } });
      definition.value = block.definition || "";
      definition.addEventListener("input", () => {
        block.definition = definition.value;
        onInput();
      });
      grid.append(term, termEn, definition);
      body.append(grid);
      return body;
    }
    if (block.type === "callout") {
      const kind = h(
        "select",
        { class: "type-select", "aria-label": "نوع کادر", dataset: { role: "callout-kind", index: String(index) } },
        ...CALLOUT_KINDS.map((value) =>
          h("option", { value: value, text: value, selected: value === block.kind ? "" : null })
        )
      );
      kind.addEventListener("change", () => {
        block.kind = kind.value;
        onInput();
      });
      const text = h("textarea", { class: "field-input", rows: "2", dataset: { role: "callout-text", index: String(index) } });
      text.value = block.text || "";
      text.addEventListener("input", () => {
        block.text = text.value;
        onInput();
      });
      body.append(kind, text);
      return body;
    }
    if (block.type === "table") {
      return tableEditor(block, onInput);
    }
    return body;
  }

  function tableEditor(block, onInput) {
    const wrap = h("div", { class: "table-editor" });
    const caption = h("input", { type: "text", class: "field-input", placeholder: "عنوان جدول (اختیاری)" });
    caption.value = block.caption || "";
    caption.addEventListener("input", () => {
      block.caption = caption.value;
      onInput();
    });
    wrap.append(caption);

    const headers = block.headers || [];
    const rows = block.rows || [];
    const columnCount = Math.max(headers.length, 1);

    const grid = h("div", { class: "table-grid", role: "table" });

    const headerRow = h("div", { class: "table-row table-head", role: "row" });
    for (let c = 0; c < columnCount; c += 1) {
      const cell = h("input", {
        type: "text",
        class: "field-input cell",
        placeholder: "ستون " + t(c + 1),
        role: "columnheader",
        dataset: { col: String(c) },
      });
      cell.value = headers[c] || "";
      cell.addEventListener("input", () => {
        block.headers[c] = cell.value;
        onInput();
      });
      headerRow.append(cell);
    }
    headerRow.append(
      h("button", { class: "icon danger", text: "✕", title: "حذف آخرین ستون", dataset: { role: "table-del-col" } })
    );
    grid.append(headerRow);

    (rows.length ? rows : [["", ""]]).forEach((row, rowIndex) => {
      const rowNode = h("div", { class: "table-row", role: "row" });
      for (let c = 0; c < columnCount; c += 1) {
        const cell = h("input", {
          type: "text",
          class: "field-input cell",
          dir: "auto",
          role: "cell",
          dataset: { row: String(rowIndex), col: String(c) },
        });
        cell.value = row[c] || "";
        cell.addEventListener("input", () => {
          block.rows[rowIndex][c] = cell.value;
          onInput();
        });
        rowNode.append(cell);
      }
      rowNode.append(
        h("button", {
          class: "icon danger",
          text: "✕",
          title: "حذف این ردیف",
          dataset: { role: "table-del-row", row: String(rowIndex) },
        })
      );
      grid.append(rowNode);
    });
    wrap.append(grid);

    const tools = h("div", { class: "block-tools" });
    tools.append(
      h("button", { class: "secondary small", text: "＋ ردیف", dataset: { role: "table-add-row" } }),
      h("button", { class: "secondary small", text: "＋ ستون", dataset: { role: "table-add-col" } })
    );
    wrap.append(tools);

    wrap.addEventListener("click", (event) => {
      const role = event.target.dataset && event.target.dataset.role;
      if (!role) return;
      if (role === "table-add-row") {
        block.rows.push(new Array(columnCount).fill(""));
        onInput();
        renderEditor();
      } else if (role === "table-add-col") {
        block.headers.push("ستون " + t(block.headers.length + 1));
        block.rows.forEach((row) => row.push(""));
        onInput();
        renderEditor();
      } else if (role === "table-del-col") {
        if (columnCount <= 1) return;
        block.headers.pop();
        block.rows.forEach((row) => row.pop());
        onInput();
        renderEditor();
      } else if (role === "table-del-row") {
        const rowIndex = Number(event.target.dataset.row);
        block.rows.splice(rowIndex, 1);
        onInput();
        renderEditor();
      }
    });
    return wrap;
  }

  function addBlockMenu(section) {
    const select = h(
      "select",
      { class: "type-select", "aria-label": "نوع بلوک جدید" },
      ...Object.keys(BLOCK_TYPES).map((type) => h("option", { value: type, text: BLOCK_TYPES[type] }))
    );
    const button = h("button", { class: "secondary small", text: "＋ افزودن بلوک" });
    button.addEventListener("click", () => {
      const type = select.value;
      const block = { type: type, text: "" };
      if (type === "definition") block.definition = "";
      if (type === "callout") block.kind = "نکته";
      if (type === "table") {
        block.caption = "";
        block.headers = ["ستون ۱", "ستون ۲"];
        block.rows = [["", ""], ["", ""]];
      }
      section.blocks.push(block);
      markDirty();
      renderEditor();
    });
    return h("div", { class: "add-block" }, select, button);
  }

  function stringListEditor(title, list, placeholder) {
    const card = h("details", { class: "card list-card" });
    card.append(h("summary", { text: title + " (" + t(list.length) + ")" }));
    const holder = h("div", { class: "list-holder" });
    list.forEach((item, index) => {
      const input = h("input", { type: "text", class: "field-input", placeholder: placeholder || "" });
      input.value = item;
      input.addEventListener("input", () => {
        list[index] = input.value;
        markDirty();
      });
      holder.append(
        h(
          "div",
          { class: "row-edit" },
          input,
          h("button", {
            class: "icon danger",
            text: "✕",
            title: "حذف",
            onclick: null,
          })
        )
      );
    });
    holder.addEventListener("click", (event) => {
      if (event.target.tagName !== "BUTTON") return;
      const index = Array.prototype.indexOf.call(holder.children, event.target.closest(".row-edit"));
      if (index >= 0) {
        list.splice(index, 1);
        markDirty();
        renderEditor();
      }
    });
    const add = h("button", { class: "secondary small", text: "＋ افزودن" });
    add.addEventListener("click", () => {
      list.push("");
      markDirty();
      renderEditor();
    });
    card.append(holder, add);
    return card;
  }

  function glossaryEditor(doc) {
    const card = h("details", { class: "card list-card" });
    card.append(h("summary", { text: "واژه‌نامه (" + t((doc.glossary || []).length) + ")" }));
    const holder = h("div", { class: "list-holder" });
    (doc.glossary || []).forEach((entry, index) => {
      const grid = h("div", { class: "def-grid" });
      const term = h("input", { type: "text", class: "field-input", placeholder: "اصطلاح" });
      term.value = entry.term || "";
      term.addEventListener("input", () => {
        entry.term = term.value;
        markDirty();
      });
      const en = h("input", { type: "text", class: "field-input", dir: "ltr", placeholder: "English (اختیاری)" });
      en.value = entry.term_en || "";
      en.addEventListener("input", () => {
        entry.term_en = en.value;
        markDirty();
      });
      const definition = h("textarea", { class: "field-input", rows: "2", placeholder: "تعریف" });
      definition.value = entry.definition || "";
      definition.addEventListener("input", () => {
        entry.definition = definition.value;
        markDirty();
      });
      grid.append(term, en, definition);
      holder.append(
        h(
          "div",
          { class: "row-edit" },
          grid,
          h("button", { class: "icon danger", text: "✕", title: "حذف" })
        )
      );
    });
    holder.addEventListener("click", (event) => {
      if (event.target.tagName !== "BUTTON") return;
      const index = Array.prototype.indexOf.call(holder.children, event.target.closest(".row-edit"));
      if (index >= 0) {
        doc.glossary.splice(index, 1);
        markDirty();
        renderEditor();
      }
    });
    const add = h("button", { class: "secondary small", text: "＋ افزودن واژه" });
    add.addEventListener("click", () => {
      doc.glossary.push({ term: "", term_en: "", definition: "" });
      markDirty();
      renderEditor();
    });
    card.append(holder, add);
    return card;
  }

  function renderEditor() {
    const body = el("editor-body");
    body.replaceChildren();
    const status = el("editor-status");
    status.hidden = true;
    status.replaceChildren();

    if (!state.editor.structured) {
      renderLegacyEditor(body);
      return;
    }
    const doc = state.editor.doc;
    if (state.editor.preview) {
      body.append(buildPreview(doc));
      return;
    }

    /* -- document header -- */
    const head = h("div", { class: "card doc-head" });
    const title = h("input", { type: "text", class: "field-input title-input", placeholder: "عنوان جزوه" });
    title.value = doc.title || "";
    title.addEventListener("input", () => {
      doc.title = title.value;
      markDirty();
    });
    const summary = h("textarea", { class: "field-input", rows: "3", placeholder: "خلاصه / زیرعنوان" });
    summary.value = doc.summary || "";
    summary.addEventListener("input", () => {
      doc.summary = summary.value;
      markDirty();
    });
    const metaChips = h("div", { class: "submission-docs" });
    if (state.editor.meta.submission_id) {
      metaChips.append(h("span", { class: "chip", text: "شناسه: GMS-" + t(state.editor.meta.submission_id) }));
    }
    if (state.editor.meta.created_at) {
      metaChips.append(h("span", { class: "chip", text: "تاریخ ایجاد: " + faDate(state.editor.meta.created_at) }));
    }
    metaChips.append(h("span", { class: "chip", text: "بخش‌ها: " + t((doc.sections || []).length) }));
    head.append(
      h("label", { class: "field" }, h("span", { text: "عنوان جزوه" }), title),
      h("label", { class: "field" }, h("span", { text: "خلاصه" }), summary),
      metaChips
    );
    body.append(head);

    /* -- sections -- */
    (doc.sections || []).forEach((section, index) => {
      const details = h("details", {
        class: "card section-card" + (state.editor.openSections[index] === false ? "" : " open"),
      });
      if (state.editor.openSections[index] !== false) details.open = true;
      details.addEventListener("toggle", () => {
        state.editor.openSections[index] = details.open;
      });
      const heading = h("input", { type: "text", class: "field-input title-input", placeholder: "عنوان بخش" });
      heading.value = section.heading || "";
      heading.addEventListener("input", () => {
        section.heading = heading.value;
        markDirty();
      });
      const summaryRow = h(
        "div",
        { class: "section-head" },
        h("summary", { class: "sr-only" }, null),
        h("span", { class: "section-index", text: "بخش " + t(index + 1) }),
        heading,
        h("span", { class: "booklet-meta", text: t(section.blocks.length) + " بلوک" }),
        h(
          "div",
          { class: "block-tools" },
          h("button", { class: "icon", text: "▲", title: "انتقال بخش به بالا", dataset: { role: "section-up", index: String(index) } }),
          h("button", { class: "icon", text: "▼", title: "انتقال بخش به پایین", dataset: { role: "section-down", index: String(index) } }),
          h("button", { class: "icon danger", text: "✕", title: "حذف بخش", dataset: { role: "section-del", index: String(index) } })
        )
      );
      const blocksHolder = h("div", { class: "blocks" });
      section.blocks.forEach((block, blockIndex) => {
        blocksHolder.append(
          h(
            "div",
            { class: "block" },
            blockToolbar(section.blocks, blockIndex, block),
            blockBody(block, blockIndex)
          )
        );
      });
      blocksHolder.addEventListener("change", (event) => {
        if (event.target.dataset && event.target.dataset.role === "block-type") {
          convertBlock(section.blocks[Number(event.target.dataset.index)], event.target.value);
        }
      });
      blocksHolder.addEventListener("click", (event) => {
        const role = event.target.dataset && event.target.dataset.role;
        if (!role || role.indexOf("block-") !== 0) return;
        const blockIndex = Number(event.target.dataset.index);
        if (role === "block-up") moveItem(section.blocks, blockIndex, -1);
        else if (role === "block-down") moveItem(section.blocks, blockIndex, 1);
        else if (role === "block-del") removeItem(section.blocks, blockIndex);
      });
      details.append(summaryRow, blocksHolder, addBlockMenu(section));
      body.append(details);
    });

    const addSection = h("button", { class: "secondary", text: "＋ افزودن بخش جدید" });
    addSection.addEventListener("click", () => {
      doc.sections.push({ heading: "بخش جدید", blocks: [{ type: "paragraph", text: "" }] });
      markDirty();
      renderEditor();
    });
    body.append(h("div", { class: "add-section" }, addSection));

    /* -- global lists -- */
    const globals = h("div", { class: "globals" });
    globals.append(
      stringListEditor("اهداف یادگیری", doc.learning_objectives, "یک هدف یادگیری"),
      stringListEditor("نکات کلیدی سند", doc.key_points, "یک نکتهٔ کلیدی"),
      stringListEditor("پرسش‌های مرور", doc.review_questions, "یک پرسش"),
      glossaryEditor(doc)
    );
    body.append(globals);
  }

  function renderLegacyEditor(body) {
    const status = el("editor-status");
    status.hidden = false;
    status.textContent =
      "این جزوه نسخهٔ ساختارمند ندارد (پیش از ویرایش ساختاری ساخته شده است)؛ ویرایش متنی ساده در دسترس است و خروجی Word از همین متن ساخته می‌شود.";
    const label = h("label", { class: "field" }, h("span", { text: "متن جزوه" }));
    const area = h("textarea", { class: "field-input legacy-area", spellcheck: "false", dir: "auto" });
    area.value = state.editor.legacyText;
    const count = h("p", { class: "hint", text: t(state.editor.legacyText.length) + " نویسه" });
    area.addEventListener("input", () => {
      state.editor.legacyText = area.value;
      count.textContent = t(area.value.length) + " نویسه";
      markDirty();
    });
    label.append(area);
    const card = h("div", { class: "card doc-head" }, label, count);
    body.append(card);
    el("editor-export-pdf").disabled = true;
  }

  /* ------------------------------------------------------------- preview */

  function buildPreview(doc) {
    const wrap = h("article", { class: "preview" });
    wrap.append(h("h1", { class: "pv-title", text: doc.title || "جزوهٔ بی‌نام" }));
    if (doc.summary) wrap.append(h("p", { class: "pv-summary", text: doc.summary }));
    if ((doc.learning_objectives || []).length) {
      const block = h("section", { class: "pv-block" });
      block.append(h("h2", { text: "اهداف یادگیری" }));
      const list = h("ul", { class: "pv-list" });
      doc.learning_objectives.forEach((item) => list.append(h("li", { text: item })));
      block.append(list);
      wrap.append(block);
    }
    (doc.sections || []).forEach((section) => {
      const block = h("section", { class: "pv-block" });
      block.append(h("h2", { text: section.heading || "بخش" }));
      let stepNumber = 0;
      section.blocks.forEach((b) => {
        if (b.type === "paragraph") block.append(h("p", { text: b.text }));
        else if (b.type === "definition") {
          block.append(
            h(
              "div",
              { class: "pv-def" },
              h("strong", { text: b.term + (b.term_en ? " (" + b.term_en + ")" : "") }),
              h("span", { text: b.definition })
            )
          );
        } else if (b.type === "bullet") block.append(h("p", { class: "pv-bullet", text: "• " + b.text }));
        else if (b.type === "example") block.append(h("div", { class: "pv-example" }, h("span", { class: "pv-label", text: "مثال" }), h("span", { text: b.text })));
        else if (b.type === "step") {
          stepNumber += 1;
          block.append(h("p", { class: "pv-step", text: t(stepNumber) + ". " + b.text }));
        }
        else if (b.type === "formula") block.append(h("div", { class: "pv-formula", text: b.text }));
        else if (b.type === "key_point") block.append(h("p", { class: "pv-key", text: "⭐ " + b.text }));
        else if (b.type === "callout") block.append(h("div", { class: "pv-callout pv-" + (b.kind || "نکته") }, h("strong", { text: b.kind || "نکته" }), h("span", { text: b.text })));
        else if (b.type === "table") {
          const table = h("table", { class: "pv-table" });
          const headRow = h("tr");
          (b.headers || []).forEach((cell) => headRow.append(h("th", { text: cell })));
          table.append(h("thead", null, headRow));
          const tbody = h("tbody");
          (b.rows || []).forEach((row) => {
            const tr = h("tr");
            row.forEach((cell) => tr.append(h("td", { text: cell })));
            tbody.append(tr);
          });
          table.append(tbody);
          if (b.caption) block.append(h("p", { class: "pv-caption", text: b.caption }));
          block.append(table);
        }
      });
      wrap.append(block);
    });
    if ((doc.key_points || []).length) {
      const block = h("section", { class: "pv-block" });
      block.append(h("h2", { text: "نکات کلیدی" }));
      const list = h("ul", { class: "pv-list" });
      doc.key_points.forEach((item) => list.append(h("li", { text: item })));
      block.append(list);
      wrap.append(block);
    }
    if ((doc.glossary || []).length) {
      const block = h("section", { class: "pv-block" });
      block.append(h("h2", { text: "واژه‌نامه" }));
      const dl = h("dl", { class: "pv-glossary" });
      doc.glossary.forEach((entry) => {
        dl.append(h("dt", { text: entry.term + (entry.term_en ? " (" + entry.term_en + ")" : "") }));
        dl.append(h("dd", { text: entry.definition }));
      });
      block.append(dl);
      wrap.append(block);
    }
    if ((doc.speakers || []).length) {
      const block = h("section", { class: "pv-block" });
      block.append(h("h2", { text: "گویندگان" }));
      const list = h("ul", { class: "pv-list" });
      doc.speakers.forEach((item) => list.append(h("li", { text: item })));
      block.append(list);
      wrap.append(block);
    }
    return wrap;
  }

  /* ------------------------------------------------------- editor actions */

  async function openEditor(itemId, options) {
    const preview = !!(options && options.preview);
    if (!confirmDiscard()) return;
    state.editor = {
      itemId: Number(itemId),
      structured: false,
      doc: null,
      legacyText: "",
      legacyTitle: "",
      meta: {},
      dirty: false,
      preview: preview,
      openSections: {},
    };
    el("editor-dirty").hidden = true;
    el("editor-heading").textContent = "ویرایش جزوه";
    el("editor-body").replaceChildren();
    el("revision-list").replaceChildren();
    el("editor-save").disabled = true;
    el("editor-export-pdf").disabled = true;
    el("editor-export-docx").disabled = true;
    setLoadState("editor", "در حال بارگذاری جزوه…", false);
    show("editor");
    try {
      const data = await call(api.booklet, { id: Number(itemId) }, "POST", { readOnly: true });
      state.editor.structured = !!data.structured;
      state.editor.meta = {
        submission_id: data.submission_id,
        created_at: data.created_at || "",
        available: data.available || [],
      };
      if (data.structured) {
        const doc = data.structured;
        doc.sections = (doc.sections || []).map((section) => ({
          heading: section.heading || "",
          blocks: sectionBlocks(section),
        }));
        doc.glossary = (doc.glossary || []).map((entry) => ({
          term: entry.term || "",
          term_en: entry.term_en || "",
          definition: entry.definition || "",
        }));
        doc.learning_objectives = doc.learning_objectives || [];
        doc.review_questions = doc.review_questions || [];
        doc.key_points = doc.key_points || [];
        doc.practical_tips = doc.practical_tips || [];
        state.editor.doc = doc;
      } else {
        state.editor.legacyText = data.text || "";
        state.editor.legacyTitle = data.title || "";
      }
      el("editor-heading").textContent =
        (data.title || "ویرایش جزوه") + (state.editor.structured ? "" : " (متنی)");
      el("editor-save").disabled = !data.editable;
      el("editor-export-pdf").disabled =
        !data.editable || (!state.editor.structured && (data.available || []).indexOf("booklet") < 0);
      el("editor-export-docx").disabled = !data.editable;
      renderEditor();
      updatePreviewButton();
      setLoadState("editor", "", false);
      loadRevisions();
    } catch (error) {
      setLoadState("editor", error.message || "بارگذاری جزوه ناموفق بود.", true);
      diagnostic("Booklet request failed" + (error.status ? " · HTTP " + error.status : ""));
    }
  }

  function updatePreviewButton() {
    const button = el("editor-preview");
    button.setAttribute("aria-pressed", state.editor.preview ? "true" : "false");
    button.textContent = state.editor.preview ? "✏️ ویرایش" : "👁 پیش‌نمایش";
    button.classList.toggle("is-active", state.editor.preview);
  }

  async function saveEditor() {
    if (!state.editor.itemId) return;
    if (state.editor.structured && !hasEditableContent(state.editor.doc)) {
      toast("جزوه خالی را نمی‌توان ذخیره کرد.");
      return;
    }
    const button = el("editor-save");
    button.disabled = true;
    try {
      let response;
      if (state.editor.structured) {
        response = await call(
          api.booklet,
          { id: state.editor.itemId, document: assembleDocument() },
          "PUT",
          { timeout: 60000 }
        );
      } else {
        const text = state.editor.legacyText.trim();
        if (!text) {
          toast("متن خالی را نمی‌توان ذخیره کرد.");
          return;
        }
        response = await call(
          api.booklet,
          { id: state.editor.itemId, text: text },
          "PUT",
          { timeout: 30000 }
        );
      }
      state.editor.dirty = false;
      el("editor-dirty").hidden = true;
      const rendered = (response && response.rendered) || [];
      toast(
        rendered.length
          ? "نسخهٔ جدید ذخیره شد و خروجی‌های " +
              (rendered.indexOf("booklet") >= 0 ? "PDF " : "") +
              (rendered.indexOf("docx") >= 0 ? "و Word " : "") +
              "بازسازی شد ✅"
          : "ذخیره شد ✅"
      );
      loadRevisions();
    } catch (error) {
      toast(error.message);
    } finally {
      button.disabled = false;
    }
  }

  async function exportFile(format) {
    if (!state.editor.itemId) return;
    if (state.editor.dirty) {
      toast("اول تغییرات را ذخیره کنید؛ خروجی از نسخهٔ ذخیره‌شده ساخته می‌شود.");
      return;
    }
    const button = el(format === "pdf" ? "editor-export-pdf" : "editor-export-docx");
    button.disabled = true;
    try {
      const blob = await callBinary(
        api.export,
        { id: state.editor.itemId, format: format },
        60000
      );
      const url = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = url;
      link.download =
        "gamas-booklet-" + state.editor.itemId + (format === "pdf" ? ".pdf" : ".docx");
      document.body.append(link);
      link.click();
      link.remove();
      window.setTimeout(() => URL.revokeObjectURL(url), 1000);
      toast(format === "pdf" ? "فایل PDF آماده شد ✅" : "فایل Word آماده شد ✅");
    } catch (error) {
      toast(error.message);
    } finally {
      button.disabled = false;
    }
  }

  async function downloadExport(itemId, format) {
    try {
      const blob = await callBinary(
        api.export,
        { id: Number(itemId), format: format },
        60000
      );
      const url = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = url;
      link.download = "gamas-booklet-" + itemId + (format === "pdf" ? ".pdf" : ".docx");
      document.body.append(link);
      link.click();
      link.remove();
      window.setTimeout(() => URL.revokeObjectURL(url), 1000);
    } catch (error) {
      toast(error.message);
    }
  }

  /* ------------------------------------------------------------ revisions */

  function renderRevisions(items) {
    const list = el("revision-list");
    list.replaceChildren();
    el("revisions-empty").hidden = items.length > 0;
    const labels = {
      studio: "ویرایش استودیو",
      mini_app: "ویرایش متنی",
      restore: "بازگردانی",
      original: "نسخهٔ اصلی",
    };
    items.forEach((item, index) => {
      const row = h("li", { class: "revision" });
      const meta = revisionMeta(item, labels);
      const actions = h("div", { class: "block-tools" });
      if (index > 0) {
        actions.append(
          h("button", {
            class: "secondary small",
            text: "↩️ بازگردانی",
            dataset: { role: "restore", id: String(item.id) },
          })
        );
      } else {
        actions.append(h("span", { class: "chip chip-ok", text: "آخرین نسخه" }));
      }
      row.append(meta, actions);
      list.append(row);
    });
  }

  function revisionMeta(item, labels) {
    const parts = [
      "#" + t(item.id),
      faDate(item.created_at),
      labels[item.source] || item.source || "",
      t(item.chars || 0) + " نویسه",
      item.structured ? "ساختارمند" : "متنی",
    ];
    return h("span", { class: "booklet-meta", text: parts.join(" · ") });
  }

  async function loadRevisions() {
    if (!state.editor.itemId) return;
    setLoadState("revision", "در حال دریافت تاریخچه…", false);
    try {
      const data = await call(
        api.revisions,
        { id: state.editor.itemId, limit: 20 },
        "POST",
        { readOnly: true, timeout: 20000 }
      );
      renderRevisions(data.items || []);
      setLoadState("revision", "", false);
    } catch (error) {
      setLoadState("revision", error.message || "دریافت تاریخچه ناموفق بود.", true);
      diagnostic("Revision request failed" + (error.status ? " · HTTP " + error.status : ""));
    }
  }

  async function restoreRevision(revisionId) {
    if (!state.editor.itemId) return;
    if (
      !window.confirm(
        "این نسخه جایگزین نسخهٔ فعلی شود؟ نسخهٔ فعلی در تاریخچه می‌ماند و فایل‌ها بازسازی می‌شوند."
      )
    ) {
      return;
    }
    try {
      await call(
        api.revision,
        { id: state.editor.itemId, revision_id: revisionId },
        "PUT",
        { timeout: 60000 }
      );
      toast("نسخه بازگردانی شد ✅");
      await openEditor(state.editor.itemId);
    } catch (error) {
      toast(error.message);
    }
  }

  /* --------------------------------------------------------------- loading */

  async function loadHome(force) {
    if (state.loaded.home && !force) return;
    setLoadState("home", "در حال دریافت خلاصهٔ حساب…", false);
    try {
      renderHome(await call(api.home, null, "POST", { readOnly: true }));
      state.loaded.home = true;
      setLoadState("home", "", false);
    } catch (error) {
      setLoadState("home", error.message || "دریافت خلاصه ناموفق بود.", true);
      if (error.status === 401) {
        updateInitAlert("نشست تلگرام نیاز به تازه‌سازی دارد", error.message, true);
      }
      diagnostic("Home request failed" + (error.status ? " · HTTP " + error.status : ""));
    }
  }

  async function loadRank(force) {
    const key = state.window;
    if (state.loaded.leaderboard[key] && !force) return;
    setLoadState("rank", "در حال دریافت رنکینگ…", false);
    try {
      const data = await call(
        api.leaderboard,
        { limit: 20, window: key },
        "POST",
        { readOnly: true }
      );
      renderRank(data);
      state.loaded.leaderboard[key] = true;
      setLoadState("rank", "", false);
    } catch (error) {
      setLoadState("rank", error.message || "دریافت رنکینگ ناموفق بود.", true);
      if (error.status === 401) updateInitAlert("نشست تلگرام نیاز به تازه‌سازی دارد", error.message, true);
      diagnostic("Leaderboard request failed" + (error.status ? " · HTTP " + error.status : ""));
    }
  }

  async function loadSubmissions(force) {
    if (state.loaded.submissions && !force) return;
    setLoadState("studio", "در حال دریافت فهرست استودیو…", false);
    try {
      const data = await call(api.submissions, { limit: 50 }, "POST", { readOnly: true });
      state.submissions = data.items || [];
      renderSubmissions(state.submissions);
      state.loaded.submissions = true;
      setLoadState("studio", "", false);
    } catch (error) {
      setLoadState("studio", error.message || "دریافت فهرست ناموفق بود.", true);
      if (error.status === 401) updateInitAlert("نشست تلگرام نیاز به تازه‌سازی دارد", error.message, true);
      diagnostic("Studio request failed" + (error.status ? " · HTTP " + error.status : ""));
    }
  }

  function loadViewData(view, force) {
    if (view === "home") loadHome(force);
    else if (view === "leaderboard") loadRank(force);
    else if (view === "studio") loadSubmissions(force);
  }

  function selectView(view) {
    if (!confirmDiscard()) return;
    show(view);
    loadViewData(view);
  }

  function navigateBack() {
    if (!confirmDiscard()) return;
    const previous = state.history.pop() || "home";
    show(previous, false);
    loadViewData(previous);
  }

  /* ------------------------------------------------------------------ boot */

  function bind() {
    const backButton = state.webApp && state.webApp.BackButton;
    if (backButton && typeof backButton.onClick === "function") {
      backButton.onClick(navigateBack);
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
    el("home-retry").addEventListener("click", () => loadHome(true));
    el("rank-retry").addEventListener("click", () => loadRank(true));
    el("studio-retry").addEventListener("click", () => loadSubmissions(true));
    el("speakers-back").addEventListener("click", navigateBack);
    el("speakers-retry").addEventListener("click", () => openSpeakers(state.speakers.submissionId));
    el("speakers-save").addEventListener("click", saveSpeakers);
    el("speaker-list").addEventListener("input", markDirty);
    el("submission-list").addEventListener("click", (event) => {
      const button = event.target.closest("button[data-role]");
      if (!button) return;
      const role = button.dataset.role;
      const id = Number(button.dataset.id);
      if (role === "edit") openEditor(id);
      else if (role === "preview") openEditor(id, { preview: true });
      else if (role === "speakers") openSpeakers(id);
      else if (role === "exports") {
        const format = button.dataset.pdf ? "pdf" : button.dataset.docx ? "docx" : "";
        if (!format || button.disabled) return;
        button.disabled = true;
        downloadExport(id, format).finally(() => { button.disabled = false; });
      }
    });
    el("editor-back").addEventListener("click", navigateBack);
    el("editor-retry").addEventListener("click", () =>
      openEditor(state.editor.itemId, { preview: state.editor.preview })
    );
    el("revision-retry").addEventListener("click", loadRevisions);
    el("editor-save").addEventListener("click", saveEditor);
    el("editor-preview").addEventListener("click", () => {
      state.editor.preview = !state.editor.preview;
      updatePreviewButton();
      renderEditor();
    });
    el("editor-export-pdf").addEventListener("click", () => exportFile("pdf"));
    el("editor-export-docx").addEventListener("click", () => exportFile("docx"));
    el("revision-list").addEventListener("click", (event) => {
      const button = event.target.closest("button[data-role='restore']");
      if (button) restoreRevision(Number(button.dataset.id));
    });
    window.addEventListener("beforeunload", (event) => {
      if (!state.editor.dirty && !state.speakers.dirty) return;
      event.preventDefault();
      event.returnValue = "";
    });
  }

  function installTelegramEvents() {
    const webApp = state.webApp;
    if (webApp && typeof webApp.onEvent === "function") {
      try {
        webApp.onEvent("themeChanged", applyTelegramTheme);
        webApp.onEvent("viewportChanged", applyTelegramViewport);
      } catch (error) {
        diagnostic("Optional Telegram events unavailable");
      }
    }
  }

  function retryTelegramInitialization() {
    const webApp = window.Telegram && window.Telegram.WebApp;
    if (webApp) {
      boot();
      return;
    }
    updateInitAlert("در حال تلاش برای اتصال به تلگرام", "دریافت SDK تلگرام دوباره آغاز شد…", true);
    const script = el("telegram-sdk");
    if (!script) return;
    const base = script.src.split("?")[0];
    script.src = base + "?retry=" + Date.now();
    window.setTimeout(() => {
      if (!state.booted && !(window.Telegram && window.Telegram.WebApp)) {
        updateInitAlert(
          "اتصال به تلگرام برقرار نشد",
          "دسترسی شبکه به telegram.org را بررسی کنید و دوباره تلاش کنید.",
          true
        );
      }
    }, 10000);
  }

  function boot() {
    if (state.booted) return;
    const webApp = window.Telegram && window.Telegram.WebApp;
    if (!webApp) {
      setLoadState("home", "", false);
      diagnostic("Telegram SDK unavailable");
      window.setTimeout(() => {
        if (!state.booted && !(window.Telegram && window.Telegram.WebApp)) {
          updateInitAlert(
            "اتصال به تلگرام برقرار نشد",
            "SDK تلگرام در دسترس نیست. اتصال اینترنت یا دسترسی به telegram.org را بررسی کنید.",
            true
          );
        }
      }, 2200);
      return;
    }

    state.webApp = webApp;
    if (!state.webAppReady && typeof webApp.ready === "function") {
      try {
        webApp.ready();
        state.webAppReady = true;
      } catch (error) {
        diagnostic("Telegram ready() call failed");
      }
    }
    if (typeof webApp.expand === "function") {
      try { webApp.expand(); } catch (error) { diagnostic("Telegram expand() unavailable"); }
    }
    if (typeof webApp.setHeaderColor === "function") {
      try { webApp.setHeaderColor("secondary_bg_color"); } catch (error) { /* optional */ }
    }
    state.initData = typeof webApp.initData === "string" ? webApp.initData : "";
    applyTelegramTheme();
    diagnostic("Telegram SDK available · v" + (webApp.version || "unknown") + " · " + (webApp.platform || "unknown"));
    diagnostic("Signed initData present · " + (state.initData ? "yes" : "no"));
    if (!state.initData) {
      setLoadState("home", "", false);
      updateInitAlert(
        "این صفحه از داخل تلگرام باز نشده است",
        "برای احراز هویت امن، برنامه را از منوی بات گاماس باز کنید؛ ورود در مرورگر عادی پشتیبانی نمی‌شود.",
        true
      );
      return;
    }

    state.booted = true;
    updateInitAlert("", "", false);
    bind();
    installTelegramEvents();
    syncBackButton();
    diagnostic("DOM ready · Home bootstrap started");
    loadHome();
  }

  function initialize() {
    const params = new URLSearchParams(window.location.search || "");
    state.diagnostics = params.get("diagnostics") === "1";
    if (state.diagnostics) el("diagnostics").hidden = false;
    el("init-retry").addEventListener("click", retryTelegramInitialization);
    const sdk = el("telegram-sdk");
    if (sdk) {
      sdk.addEventListener("load", () => {
        diagnostic("Telegram SDK script loaded");
        boot();
      });
      sdk.addEventListener("error", () => {
        diagnostic("Telegram SDK script failed");
        updateInitAlert(
          "اتصال به تلگرام برقرار نشد",
          "فایل SDK از telegram.org بارگیری نشد. اتصال شبکه را بررسی کرده و تلاش دوباره را بزنید.",
          true
        );
      });
    }
    window.addEventListener("error", (event) => {
      diagnostic("JavaScript exception" + (event && event.lineno ? " · line " + event.lineno : ""));
    });
    window.addEventListener("unhandledrejection", () => {
      diagnostic("Unhandled JavaScript promise rejection");
    });
    diagnostic("DOM ready");
    boot();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", initialize);
  } else {
    initialize();
  }
})();
