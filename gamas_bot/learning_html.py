"""Deterministic offline interactive HTML renderer for quizzes and flashcards.

Both deliverables are single, self-contained Persian RTL HTML files with:
- Embedded Vazirmatn Regular & Bold fonts (``data:font/ttf;base64,...``).
- Strict Content-Security-Policy (``default-src 'none'`` + SHA-256 pinned JS).
- Zero external network requests and zero ``innerHTML`` writes from data.
- Safe JSON payload embedded in a non-executable ``application/json`` tag.
"""

from __future__ import annotations

import base64
import html
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Final, Literal

from .docx_export import sanitize_filename_part
from .pdf_common import find_font
from .progress import to_persian_digits
from .review_sheet_html import script_hash

LearningHtmlKind = Literal["quiz", "flashcards"]

MAX_LEARNING_HTML_BYTES: Final = 4 * 1024 * 1024

_DATA_ELEMENT_ID: Final = "gamas-learning-data"


class LearningHtmlRenderError(RuntimeError):
    """Raised when interactive learning HTML cannot be rendered safely."""


@dataclass(frozen=True, slots=True)
class LearningHtmlArtifact:
    """Rendered offline interactive HTML document ready for storage and delivery."""

    filename: str
    content: bytes
    kind: LearningHtmlKind
    item_count: int


@lru_cache(maxsize=1)
def _embedded_font_css() -> str:
    """Return base64 ``@font-face`` rules for Vazirmatn Regular and Bold."""
    rules: list[str] = []
    for weight, filename in ((400, "Vazirmatn-Regular.ttf"), (700, "Vazirmatn-Bold.ttf")):
        path = find_font(filename)
        if path is None:
            raise LearningHtmlRenderError(f"font file {filename} not found")
        encoded = base64.b64encode(path.read_bytes()).decode("ascii")
        rules.append(
            "@font-face{font-family:'Vazirmatn';font-style:normal;font-weight:"
            f"{weight};font-display:block;"
            f"src:url(data:font/ttf;base64,{encoded}) format('truetype');}}"
        )
    return "".join(rules)


def learning_html_csp(script_body: str) -> str:
    """Strict offline CSP pinning the single deterministic inline script."""
    return (
        "default-src 'none'; "
        "style-src 'unsafe-inline'; "
        f"script-src {script_hash(script_body)}; "
        "font-src data:; img-src data:; "
        "base-uri 'none'; form-action 'none'; object-src 'none'; frame-ancestors 'none'"
    )


def _safe_json_for_script(payload: dict[str, Any]) -> str:
    """Serialize JSON safely for embedding inside a ``<script>`` tag."""
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return (
        raw.replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("&", "\\u0026")
        .replace("\u2028", "\\u2028")
        .replace("\u2029", "\\u2029")
    )


def _esc(value: object) -> str:
    return html.escape(str(value), quote=True)


def _clean_text(value: object, *, limit: int = 2000) -> str:
    text = " ".join(str(value or "").split()).strip()
    return text[:limit]


def _artifact_filename(prefix: str, title: str) -> str:
    slug = sanitize_filename_part(title, max_chars=48) or prefix
    if slug.lower().startswith(f"{prefix}-"):
        return f"{slug}.html"
    return f"{prefix}-{slug}.html"


# ---------------------------------------------------------------------------
# Shared stylesheet (light parchment + deep emerald dark mode, RTL-first)
# ---------------------------------------------------------------------------

_SHARED_CSS: Final = """
:root{
  --bg:#FAF9F5;--surface:#FFFFFF;--surface-2:#F3F0E7;--surface-3:#EAE5D8;
  --border:#E4DFD2;--border-strong:#CFC8B8;
  --text:#1B2420;--text-muted:#55625C;--text-soft:#73807A;
  --accent:#1D6F5A;--accent-hover:#165847;--accent-soft:#E4F3EE;--accent-border:#9FD5C5;
  --ok:#1A7F4B;--ok-soft:#E3F6EC;--ok-border:#8FD7B0;
  --bad:#B93131;--bad-soft:#FDECEC;--bad-border:#F0A9A9;
  --warn:#A6620C;--warn-soft:#FDF3E2;--warn-border:#EBC88B;
  --shadow:0 10px 30px rgba(27,36,32,.07);--shadow-sm:0 3px 10px rgba(27,36,32,.05);
  --radius:18px;--radius-sm:12px;--font:'Vazirmatn',system-ui,-apple-system,sans-serif;
}
html[data-theme="dark"]{
  --bg:#0E1513;--surface:#151F1C;--surface-2:#1B2824;--surface-3:#22332E;
  --border:#273934;--border-strong:#364E47;
  --text:#EDF3F0;--text-muted:#A9BBB4;--text-soft:#7E958C;
  --accent:#38B28E;--accent-hover:#4AC4A0;--accent-soft:#15362D;--accent-border:#256654;
  --ok:#3CC27C;--ok-soft:#133524;--ok-border:#236B46;
  --bad:#EE6B6B;--bad-soft:#3B191B;--bad-border:#783034;
  --warn:#E9A844;--warn-soft:#392913;--warn-border:#73501E;
  --shadow:0 12px 34px rgba(0,0,0,.38);--shadow-sm:0 4px 12px rgba(0,0,0,.24);
}
*{box-sizing:border-box;-webkit-tap-highlight-color:transparent;}
html,body{margin:0;padding:0;background:var(--bg);color:var(--text);font-family:var(--font);
  line-height:1.8;min-height:100vh;font-size:16px;-webkit-font-smoothing:antialiased;}
button,input{font-family:inherit;color:inherit;}
button{cursor:pointer;border:none;background:none;}
.shell{max-width:860px;margin:0 auto;padding:18px 16px 56px;}
.topbar{display:flex;align-items:center;justify-content:space-between;gap:12px;margin-bottom:16px;}
.brand{display:inline-flex;align-items:center;gap:8px;font-size:13px;font-weight:700;
  color:var(--accent);background:var(--accent-soft);border:1px solid var(--accent-border);
  padding:5px 12px;border-radius:999px;}
.icon-btn{display:inline-flex;align-items:center;justify-content:center;gap:6px;
  background:var(--surface);border:1px solid var(--border);border-radius:999px;
  padding:7px 14px;font-size:13px;font-weight:700;color:var(--text-muted);
  box-shadow:var(--shadow-sm);transition:all .16s ease;}
.icon-btn:hover{border-color:var(--accent-border);color:var(--text);transform:translateY(-1px);}
.hero{background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);
  padding:20px 22px;box-shadow:var(--shadow);margin-bottom:16px;}
.hero-title{margin:0 0 6px;font-size:clamp(20px,3vw,26px);line-height:1.45;font-weight:700;}
.hero-sub{margin:0;font-size:13.5px;color:var(--text-muted);}
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(120px,1fr));gap:10px;margin-top:16px;}
.stat{background:var(--surface-2);border:1px solid var(--border);border-radius:var(--radius-sm);
  padding:10px 12px;text-align:center;}
.stat-val{font-size:20px;font-weight:700;line-height:1.3;color:var(--text);}
.stat-lbl{font-size:12px;color:var(--text-muted);margin-top:2px;}
.stat.ok .stat-val{color:var(--ok);}
.stat.bad .stat-val{color:var(--bad);}
.stat.warn .stat-val{color:var(--warn);}
.stat.acc .stat-val{color:var(--accent);}
.pbar{height:8px;background:var(--surface-3);border-radius:999px;overflow:hidden;margin-top:14px;}
.pfill{height:100%;background:var(--accent);border-radius:999px;transition:width .25s ease;width:0%;}
.toolbar{display:flex;flex-wrap:wrap;align-items:center;justify-content:space-between;gap:8px;margin-bottom:14px;}
.pill-group{display:flex;flex-wrap:wrap;gap:6px;}
.pill{padding:6px 13px;border-radius:999px;font-size:13px;font-weight:700;
  background:var(--surface);border:1px solid var(--border);color:var(--text-muted);
  transition:all .15s ease;}
.pill:hover{border-color:var(--border-strong);color:var(--text);}
.pill.active{background:var(--accent);border-color:var(--accent);color:#fff;}
.card{background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);
  padding:22px;box-shadow:var(--shadow);margin-bottom:14px;}
.badge{display:inline-flex;align-items:center;gap:5px;font-size:12px;font-weight:700;
  padding:3px 10px;border-radius:999px;background:var(--surface-2);color:var(--text-muted);
  border:1px solid var(--border);}
.badge.ok{background:var(--ok-soft);color:var(--ok);border-color:var(--ok-border);}
.badge.bad{background:var(--bad-soft);color:var(--bad);border-color:var(--bad-border);}
.badge.warn{background:var(--warn-soft);color:var(--warn);border-color:var(--warn-border);}
.badge.acc{background:var(--accent-soft);color:var(--accent);border-color:var(--accent-border);}
.btn{display:inline-flex;align-items:center;justify-content:center;gap:8px;
  padding:11px 18px;border-radius:var(--radius-sm);font-size:14.5px;font-weight:700;
  transition:all .16s ease;border:1px solid transparent;}
.btn:disabled{opacity:.42;cursor:not-allowed;transform:none;}
.btn-primary{background:var(--accent);color:#fff;}
.btn-primary:hover:not(:disabled){background:var(--accent-hover);transform:translateY(-1px);}
.btn-secondary{background:var(--surface-2);border-color:var(--border);color:var(--text);}
.btn-secondary:hover:not(:disabled){border-color:var(--border-strong);background:var(--surface-3);}
.btn-ok{background:var(--ok);color:#fff;}
.btn-ok:hover:not(:disabled){filter:brightness(1.06);transform:translateY(-1px);}
.btn-warn{background:var(--warn-soft);border-color:var(--warn-border);color:var(--warn);}
.btn-warn:hover:not(:disabled){filter:brightness(.97);transform:translateY(-1px);}
.nav-row{display:flex;align-items:center;justify-content:space-between;gap:10px;margin-top:16px;flex-wrap:wrap;}
.dots{display:flex;flex-wrap:wrap;gap:6px;margin-bottom:14px;}
.dot{min-width:34px;height:34px;padding:0 8px;border-radius:10px;font-size:13px;font-weight:700;
  background:var(--surface);border:1px solid var(--border);color:var(--text-muted);
  display:inline-flex;align-items:center;justify-content:center;transition:all .14s ease;}
.dot:hover{border-color:var(--accent-border);color:var(--text);}
.dot.cur{border-color:var(--accent);box-shadow:0 0 0 2px var(--accent-soft);color:var(--accent);}
.dot.ok{background:var(--ok-soft);border-color:var(--ok-border);color:var(--ok);}
.dot.bad{background:var(--bad-soft);border-color:var(--bad-border);color:var(--bad);}
.q-head{display:flex;align-items:center;justify-content:space-between;gap:8px;margin-bottom:12px;flex-wrap:wrap;}
.q-stem{font-size:17.5px;font-weight:700;line-height:1.75;margin:0 0 18px;white-space:pre-wrap;}
.opts{display:flex;flex-direction:column;gap:10px;}
.opt{display:flex;align-items:flex-start;gap:12px;width:100%;text-align:right;
  padding:13px 15px;border-radius:var(--radius-sm);background:var(--surface-2);
  border:1.5px solid var(--border);font-size:15.5px;line-height:1.7;transition:all .15s ease;}
.opt:hover:not(:disabled){border-color:var(--accent);background:var(--accent-soft);}
.opt:disabled{cursor:default;}
.opt-key{flex:0 0 28px;height:28px;border-radius:8px;background:var(--surface);
  border:1px solid var(--border-strong);display:inline-flex;align-items:center;
  justify-content:center;font-size:13px;font-weight:700;color:var(--text-muted);margin-top:2px;}
.opt-txt{flex:1;white-space:pre-wrap;}
.opt.correct{background:var(--ok-soft);border-color:var(--ok);color:var(--text);}
.opt.correct .opt-key{background:var(--ok);border-color:var(--ok);color:#fff;}
.opt.wrong{background:var(--bad-soft);border-color:var(--bad);color:var(--text);}
.opt.wrong .opt-key{background:var(--bad);border-color:var(--bad);color:#fff;}
.exp{margin-top:16px;padding:14px 16px;border-radius:var(--radius-sm);
  background:var(--surface-2);border-right:4px solid var(--accent);font-size:14.5px;line-height:1.8;}
.exp.ok{border-right-color:var(--ok);background:var(--ok-soft);}
.exp.bad{border-right-color:var(--bad);background:var(--bad-soft);}
.exp-title{font-weight:700;margin-bottom:6px;display:flex;align-items:center;gap:6px;}
.src-chip{display:inline-flex;align-items:center;gap:5px;margin-top:10px;
  font-size:12.5px;color:var(--text-muted);background:var(--surface);
  border:1px solid var(--border);border-radius:999px;padding:3px 10px;}
.result-box{text-align:center;padding:28px 20px;}
.score-ring{display:inline-flex;flex-direction:column;align-items:center;justify-content:center;
  width:132px;height:132px;border-radius:50%;background:var(--accent-soft);
  border:4px solid var(--accent);margin-bottom:14px;}
.score-pct{font-size:32px;font-weight:700;color:var(--accent);line-height:1.1;}
.score-sub{font-size:12.5px;color:var(--text-muted);}
.result-title{margin:4px 0 8px;font-size:22px;font-weight:700;}
.result-actions{display:flex;flex-wrap:wrap;justify-content:center;gap:10px;margin-top:18px;}
/* Flashcards */
.fc-stage{perspective:1200px;margin-bottom:14px;}
.fc-card{min-height:260px;background:var(--surface);border:1.5px solid var(--border);
  border-radius:22px;padding:26px 24px;box-shadow:var(--shadow);cursor:pointer;
  display:flex;flex-direction:column;justify-content:space-between;gap:16px;
  transition:transform .2s ease,border-color .2s ease,background .2s ease;position:relative;}
.fc-card:hover{border-color:var(--accent-border);}
.fc-card.flipped{background:var(--surface-2);border-color:var(--accent);}
.fc-top{display:flex;align-items:center;justify-content:space-between;gap:8px;flex-wrap:wrap;}
.fc-body{font-size:clamp(17.5px,2.6vw,21px);line-height:1.85;font-weight:700;
  text-align:center;margin:14px 0;white-space:pre-wrap;}
.fc-card.flipped .fc-body{font-weight:400;font-size:clamp(16px,2.2vw,18.5px);text-align:right;}
.fc-hint{text-align:center;font-size:12.5px;color:var(--text-soft);}
.fc-actions{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-top:12px;}
.fc-actions.single{grid-template-columns:1fr;}
.search-box{width:100%;padding:11px 14px;border-radius:var(--radius-sm);
  background:var(--surface);border:1px solid var(--border);font-size:14.5px;margin-bottom:12px;}
.search-box:focus{outline:none;border-color:var(--accent);}
.list-item{background:var(--surface);border:1px solid var(--border);border-radius:var(--radius-sm);
  padding:15px 16px;margin-bottom:10px;box-shadow:var(--shadow-sm);}
.list-q{font-weight:700;font-size:15.5px;margin-bottom:8px;white-space:pre-wrap;}
.list-a{font-size:14.5px;color:var(--text-muted);padding-top:8px;border-top:1px dashed var(--border);
  white-space:pre-wrap;}
.list-a.hidden{display:none;}
.empty{text-align:center;padding:32px 18px;color:var(--text-muted);}
.footer{text-align:center;font-size:12px;color:var(--text-soft);margin-top:24px;}
@media (max-width:600px){
  .shell{padding:12px 12px 44px;}
  .hero,.card,.fc-card{padding:16px;}
  .fc-actions{grid-template-columns:1fr;}
}
@media print{
  .topbar,.toolbar,.dots,.nav-row,.fc-actions,.icon-btn,.footer{display:none !important;}
  .card,.hero,.list-item{box-shadow:none !important;break-inside:avoid;}
}
"""


# ---------------------------------------------------------------------------
# Deterministic client-side JS (zero innerHTML from data; CSP SHA-256 pinned)
# ---------------------------------------------------------------------------

_QUIZ_SCRIPT: Final = r"""
(function(){
  'use strict';
  var DATA = JSON.parse(document.getElementById('gamas-learning-data').textContent);
  var FA = ['۰','۱','۲','۳','۴','۵','۶','۷','۸','۹'];
  var LETTERS = ['الف','ب','ج','د'];
  function fa(n){return String(n).replace(/[0-9]/g,function(d){return FA[Number(d)];});}
  function el(tag, cls, text){
    var node = document.createElement(tag);
    if(cls) node.className = cls;
    if(text !== undefined && text !== null) node.textContent = String(text);
    return node;
  }
  var root = document.getElementById('app');
  var themeBtn = document.getElementById('theme-btn');

  function applyTheme(theme){
    document.documentElement.setAttribute('data-theme', theme);
    themeBtn.textContent = theme === 'dark' ? '☀️ حالت روشن' : '🌙 حالت شب';
    try{ localStorage.setItem('gamas_theme', theme); }catch(_e){}
  }
  var initTheme = 'light';
  try{
    var saved = localStorage.getItem('gamas_theme');
    if(saved === 'dark' || saved === 'light') initTheme = saved;
    else if(window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches) initTheme = 'dark';
  }catch(_e){}
  applyTheme(initTheme);
  themeBtn.addEventListener('click', function(){
    applyTheme(document.documentElement.getAttribute('data-theme') === 'dark' ? 'light' : 'dark');
  });

  var allItems = DATA.questions.map(function(q, i){
    return {
      id: i, stem: q.stem, options: q.options.slice(), correct: q.correct,
      explanation: q.explanation || '', source: q.source || '', difficulty: q.difficulty || 'medium'
    };
  });
  var state = {
    order: allItems.map(function(q){return q.id;}),
    answers: {},
    index: 0,
    view: 'step',
    filter: 'all',
    finished: false
  };

  function visibleIds(){
    return state.order.filter(function(id){
      var q = allItems[id];
      if(state.filter === 'all') return true;
      if(state.filter === 'wrong') return state.answers[id] !== undefined && state.answers[id] !== q.correct;
      return q.difficulty === state.filter;
    });
  }

  function stats(){
    var ids = visibleIds();
    var answered = 0, correct = 0, wrong = 0;
    ids.forEach(function(id){
      if(state.answers[id] !== undefined){
        answered++;
        if(state.answers[id] === allItems[id].correct) correct++;
        else wrong++;
      }
    });
    return {total: ids.length, answered: answered, correct: correct, wrong: wrong};
  }

  function shuffleArray(arr){
    var copy = arr.slice();
    for(var i = copy.length - 1; i > 0; i--){
      var j = Math.floor(Math.random() * (i + 1));
      var tmp = copy[i]; copy[i] = copy[j]; copy[j] = tmp;
    }
    return copy;
  }

  function diffLabel(d){
    return d === 'hard' ? 'سخت' : 'متوسط';
  }

  function buildQuestionCard(q, pos, total){
    var card = el('section', 'card');
    var head = el('div', 'q-head');
    head.appendChild(el('span', 'badge acc', 'سؤال ' + fa(pos + 1) + ' از ' + fa(total)));
    head.appendChild(el('span', 'badge ' + (q.difficulty === 'hard' ? 'warn' : ''), 'سطح: ' + diffLabel(q.difficulty)));
    card.appendChild(head);

    var stem = el('h2', 'q-stem', q.stem);
    stem.setAttribute('dir', 'auto');
    card.appendChild(stem);

    var chosen = state.answers[q.id];
    var isAnswered = chosen !== undefined;
    var opts = el('div', 'opts');
    q.options.forEach(function(optText, idx){
      var btn = el('button', 'opt');
      btn.type = 'button';
      btn.disabled = isAnswered;
      if(isAnswered){
        if(idx === q.correct) btn.classList.add('correct');
        else if(idx === chosen) btn.classList.add('wrong');
      }
      btn.appendChild(el('span', 'opt-key', LETTERS[idx] || fa(idx + 1)));
      var txt = el('span', 'opt-txt', optText);
      txt.setAttribute('dir', 'auto');
      btn.appendChild(txt);
      btn.addEventListener('click', function(){
        if(state.answers[q.id] !== undefined) return;
        state.answers[q.id] = idx;
        render();
      });
      opts.appendChild(btn);
    });
    card.appendChild(opts);

    if(isAnswered){
      var ok = chosen === q.correct;
      var exp = el('div', 'exp ' + (ok ? 'ok' : 'bad'));
      exp.appendChild(el('div', 'exp-title', ok ? '✅ پاسخ درست بود!' : '❌ پاسخ درست: گزینهٔ ' + (LETTERS[q.correct] || fa(q.correct + 1))));
      if(q.explanation){
        var body = el('div', '', q.explanation);
        body.setAttribute('dir', 'auto');
        exp.appendChild(body);
      }
      if(q.source){
        exp.appendChild(el('div', 'src-chip', '📍 منبع: ' + q.source));
      }
      card.appendChild(exp);
    }
    return card;
  }

  function render(){
    root.textContent = '';
    var ids = visibleIds();
    var st = stats();
    if(state.index >= ids.length) state.index = Math.max(0, ids.length - 1);

    /* Stats inside hero */
    var statsHost = document.getElementById('hero-stats');
    statsHost.textContent = '';
    var cells = [
      {val: fa(st.answered) + ' / ' + fa(st.total), lbl: 'پاسخ‌داده‌شده', cls: 'acc'},
      {val: fa(st.correct), lbl: 'پاسخ درست', cls: 'ok'},
      {val: fa(st.wrong), lbl: 'پاسخ نادرست', cls: 'bad'},
      {val: st.answered ? fa(Math.round(st.correct * 100 / st.answered)) + '٪' : '—', lbl: 'دقت تا الان', cls: ''}
    ];
    cells.forEach(function(c){
      var box = el('div', 'stat ' + c.cls);
      box.appendChild(el('div', 'stat-val', c.val));
      box.appendChild(el('div', 'stat-lbl', c.lbl));
      statsHost.appendChild(box);
    });
    document.getElementById('hero-pfill').style.width = (st.total ? (st.answered * 100 / st.total) : 0) + '%';

    /* Toolbar */
    var bar = el('div', 'toolbar');
    var modeGroup = el('div', 'pill-group');
    var stepBtn = el('button', 'pill' + (state.view === 'step' && !state.finished ? ' active' : ''), '🎯 تک‌سؤالی');
    stepBtn.type = 'button';
    stepBtn.addEventListener('click', function(){ state.view = 'step'; state.finished = false; render(); });
    var allBtn = el('button', 'pill' + (state.view === 'all' && !state.finished ? ' active' : ''), '📋 همه سؤالات');
    allBtn.type = 'button';
    allBtn.addEventListener('click', function(){ state.view = 'all'; state.finished = false; render(); });
    var repBtn = el('button', 'pill' + (state.finished ? ' active' : ''), '📊 کارنامه');
    repBtn.type = 'button';
    repBtn.addEventListener('click', function(){ state.finished = true; render(); });
    modeGroup.appendChild(stepBtn);
    modeGroup.appendChild(allBtn);
    modeGroup.appendChild(repBtn);
    bar.appendChild(modeGroup);

    var actGroup = el('div', 'pill-group');
    var hasHard = allItems.some(function(q){return q.difficulty === 'hard';});
    var hasMed = allItems.some(function(q){return q.difficulty === 'medium';});
    if(hasHard && hasMed){
      [['all','همه'],['medium','متوسط'],['hard','سخت']].forEach(function(pair){
        var p = el('button', 'pill' + (state.filter === pair[0] ? ' active' : ''), pair[1]);
        p.type = 'button';
        p.addEventListener('click', function(){ state.filter = pair[0]; state.index = 0; state.finished = false; render(); });
        actGroup.appendChild(p);
      });
    }
    var shufBtn = el('button', 'pill', '🔀 بر زدن');
    shufBtn.type = 'button';
    shufBtn.addEventListener('click', function(){
      state.order = shuffleArray(state.order);
      state.index = 0; state.finished = false; render();
    });
    var rstBtn = el('button', 'pill', '🔄 شروع مجدد');
    rstBtn.type = 'button';
    rstBtn.addEventListener('click', function(){
      state.answers = {}; state.filter = 'all'; state.index = 0; state.finished = false; render();
    });
    actGroup.appendChild(shufBtn);
    actGroup.appendChild(rstBtn);
    bar.appendChild(actGroup);
    root.appendChild(bar);

    if(state.finished){
      var box = el('section', 'card result-box');
      var pct = st.total ? Math.round(st.correct * 100 / st.total) : 0;
      var ring = el('div', 'score-ring');
      ring.appendChild(el('div', 'score-pct', fa(pct) + '٪'));
      ring.appendChild(el('div', 'score-sub', fa(st.correct) + ' از ' + fa(st.total)));
      box.appendChild(ring);
      var verdict = pct >= 85 ? 'عالی! تسلط خیلی خوبی داری 🎉'
        : pct >= 60 ? 'خوب بود! با کمی مرور عالی می‌شی 💪'
        : 'وقت مرور دقیق‌تر نکات کلیدی است 📚';
      box.appendChild(el('h2', 'result-title', verdict));
      box.appendChild(el('p', 'hero-sub',
        'درست: ' + fa(st.correct) + '  |  نادرست: ' + fa(st.wrong) + '  |  بی‌پاسخ: ' + fa(st.total - st.answered)
      ));
      var actions = el('div', 'result-actions');
      var wrongCount = allItems.filter(function(q){
        return state.answers[q.id] !== undefined && state.answers[q.id] !== q.correct;
      }).length;
      if(wrongCount > 0){
        var retryWrong = el('button', 'btn btn-warn', '🔁 مرور فقط اشتباه‌ها (' + fa(wrongCount) + ')');
        retryWrong.type = 'button';
        retryWrong.addEventListener('click', function(){
          allItems.forEach(function(q){
            if(state.answers[q.id] !== undefined && state.answers[q.id] !== q.correct){
              delete state.answers[q.id];
            }
          });
          var wrongIds = allItems.filter(function(q){ return state.answers[q.id] === undefined; }).map(function(q){return q.id;});
          state.order = wrongIds.concat(state.order.filter(function(id){ return wrongIds.indexOf(id) === -1; }));
          state.filter = 'all'; state.index = 0; state.finished = false; state.view = 'step'; render();
        });
        actions.appendChild(retryWrong);
      }
      var again = el('button', 'btn btn-primary', '🔄 آزمون دوباره از اول');
      again.type = 'button';
      again.addEventListener('click', function(){
        state.answers = {}; state.filter = 'all'; state.index = 0; state.finished = false; state.view = 'step'; render();
      });
      actions.appendChild(again);
      box.appendChild(actions);
      root.appendChild(box);
      return;
    }

    if(!ids.length){
      var empty = el('div', 'card empty', 'سؤالی در این فیلتر وجود ندارد.');
      root.appendChild(empty);
      return;
    }

    if(state.view === 'all'){
      ids.forEach(function(id, idx){
        root.appendChild(buildQuestionCard(allItems[id], idx, ids.length));
      });
      return;
    }

    /* Question navigator dots */
    var dots = el('div', 'dots');
    ids.forEach(function(id, idx){
      var d = el('button', 'dot', fa(idx + 1));
      d.type = 'button';
      if(idx === state.index) d.classList.add('cur');
      if(state.answers[id] !== undefined){
        d.classList.add(state.answers[id] === allItems[id].correct ? 'ok' : 'bad');
      }
      d.addEventListener('click', function(){ state.index = idx; render(); });
      dots.appendChild(d);
    });
    root.appendChild(dots);

    var curQ = allItems[ids[state.index]];
    var card = buildQuestionCard(curQ, state.index, ids.length);
    var nav = el('div', 'nav-row');
    var prev = el('button', 'btn btn-secondary', '→ سؤال قبلی');
    prev.type = 'button';
    prev.disabled = state.index === 0;
    prev.addEventListener('click', function(){ if(state.index > 0){ state.index--; render(); } });
    var isLast = state.index === ids.length - 1;
    var next = el('button', 'btn btn-primary', isLast ? '📊 مشاهده کارنامه' : 'سؤال بعدی ←');
    next.type = 'button';
    next.addEventListener('click', function(){
      if(isLast){ state.finished = true; render(); }
      else { state.index++; render(); }
    });
    nav.appendChild(prev);
    nav.appendChild(next);
    card.appendChild(nav);
    root.appendChild(card);
  }

  document.addEventListener('keydown', function(e){
    if(state.finished || state.view !== 'step') return;
    var ids = visibleIds();
    if(!ids.length) return;
    var curQ = allItems[ids[state.index]];
    if(e.key >= '1' && e.key <= '4'){
      var idx = Number(e.key) - 1;
      if(idx < curQ.options.length && state.answers[curQ.id] === undefined){
        state.answers[curQ.id] = idx;
        render();
      }
    } else if(e.key === 'ArrowLeft'){
      if(state.index < ids.length - 1){ state.index++; render(); }
    } else if(e.key === 'ArrowRight'){
      if(state.index > 0){ state.index--; render(); }
    }
  });

  render();
})();
"""


_FLASHCARDS_SCRIPT: Final = r"""
(function(){
  'use strict';
  var DATA = JSON.parse(document.getElementById('gamas-learning-data').textContent);
  var FA = ['۰','۱','۲','۳','۴','۵','۶','۷','۸','۹'];
  function fa(n){return String(n).replace(/[0-9]/g,function(d){return FA[Number(d)];});}
  function el(tag, cls, text){
    var node = document.createElement(tag);
    if(cls) node.className = cls;
    if(text !== undefined && text !== null) node.textContent = String(text);
    return node;
  }
  var root = document.getElementById('app');
  var themeBtn = document.getElementById('theme-btn');

  function applyTheme(theme){
    document.documentElement.setAttribute('data-theme', theme);
    themeBtn.textContent = theme === 'dark' ? '☀️ حالت روشن' : '🌙 حالت شب';
    try{ localStorage.setItem('gamas_theme', theme); }catch(_e){}
  }
  var initTheme = 'light';
  try{
    var saved = localStorage.getItem('gamas_theme');
    if(saved === 'dark' || saved === 'light') initTheme = saved;
    else if(window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches) initTheme = 'dark';
  }catch(_e){}
  applyTheme(initTheme);
  themeBtn.addEventListener('click', function(){
    applyTheme(document.documentElement.getAttribute('data-theme') === 'dark' ? 'light' : 'dark');
  });

  var allCards = DATA.cards.map(function(c, i){
    return {id: i, front: c.front, back: c.back, source: c.source || ''};
  });
  var state = {
    order: allCards.map(function(c){return c.id;}),
    status: {},
    index: 0,
    flipped: false,
    mode: 'study',
    filter: 'all',
    query: '',
    revealAll: true
  };

  function visibleIds(){
    return state.order.filter(function(id){
      var st = state.status[id] || 'new';
      if(state.filter === 'all') return true;
      if(state.filter === 'review') return st !== 'known';
      return st === state.filter;
    });
  }

  function shuffleArray(arr){
    var copy = arr.slice();
    for(var i = copy.length - 1; i > 0; i--){
      var j = Math.floor(Math.random() * (i + 1));
      var tmp = copy[i]; copy[i] = copy[j]; copy[j] = tmp;
    }
    return copy;
  }

  function counts(){
    var known = 0, review = 0;
    allCards.forEach(function(c){
      if(state.status[c.id] === 'known') known++;
      else if(state.status[c.id] === 'review') review++;
    });
    return {total: allCards.length, known: known, review: review, left: allCards.length - known};
  }

  function markCard(id, value){
    state.status[id] = value;
    state.flipped = false;
    var ids = visibleIds();
    if(ids.length === 0){ state.index = 0; }
    else if(state.filter === 'review' && value === 'known'){
      if(state.index >= ids.length) state.index = 0;
    } else {
      state.index = (state.index + 1) % ids.length;
    }
    render();
  }

  function render(){
    root.textContent = '';
    var c = counts();
    var ids = visibleIds();
    if(state.index >= ids.length) state.index = Math.max(0, ids.length - 1);

    var statsHost = document.getElementById('hero-stats');
    statsHost.textContent = '';
    [
      {val: fa(c.total), lbl: 'کل کارت‌ها', cls: 'acc'},
      {val: fa(c.known), lbl: 'بلدم', cls: 'ok'},
      {val: fa(c.review), lbl: 'نیاز به مرور', cls: 'warn'},
      {val: fa(c.left), lbl: 'باقی‌مانده', cls: ''}
    ].forEach(function(item){
      var box = el('div', 'stat ' + item.cls);
      box.appendChild(el('div', 'stat-val', item.val));
      box.appendChild(el('div', 'stat-lbl', item.lbl));
      statsHost.appendChild(box);
    });
    document.getElementById('hero-pfill').style.width = (c.total ? (c.known * 100 / c.total) : 0) + '%';

    /* Toolbar */
    var bar = el('div', 'toolbar');
    var modeGroup = el('div', 'pill-group');
    var studyBtn = el('button', 'pill' + (state.mode === 'study' ? ' active' : ''), '🎴 حالت مطالعه');
    studyBtn.type = 'button';
    studyBtn.addEventListener('click', function(){ state.mode = 'study'; render(); });
    var listBtn = el('button', 'pill' + (state.mode === 'list' ? ' active' : ''), '📋 فهرست و جستجو');
    listBtn.type = 'button';
    listBtn.addEventListener('click', function(){ state.mode = 'list'; render(); });
    modeGroup.appendChild(studyBtn);
    modeGroup.appendChild(listBtn);
    bar.appendChild(modeGroup);

    var filterGroup = el('div', 'pill-group');
    [['all','همه'],['review','نیاز به مرور'],['known','بلدم']].forEach(function(pair){
      var p = el('button', 'pill' + (state.filter === pair[0] ? ' active' : ''), pair[1]);
      p.type = 'button';
      p.addEventListener('click', function(){ state.filter = pair[0]; state.index = 0; state.flipped = false; render(); });
      filterGroup.appendChild(p);
    });
    var shuf = el('button', 'pill', '🔀 بر زدن');
    shuf.type = 'button';
    shuf.addEventListener('click', function(){ state.order = shuffleArray(state.order); state.index = 0; state.flipped = false; render(); });
    var rst = el('button', 'pill', '🔄 ریست');
    rst.type = 'button';
    rst.addEventListener('click', function(){ state.status = {}; state.filter = 'all'; state.index = 0; state.flipped = false; render(); });
    filterGroup.appendChild(shuf);
    filterGroup.appendChild(rst);
    bar.appendChild(filterGroup);
    root.appendChild(bar);

    if(state.mode === 'list'){
      var search = el('input', 'search-box');
      search.type = 'search';
      search.placeholder = 'جستجو در پرسش، پاسخ یا منبع…';
      search.value = state.query;
      search.addEventListener('input', function(){ state.query = search.value; renderListItems(listHost); });
      root.appendChild(search);

      var listBar = el('div', 'toolbar');
      var tog = el('button', 'pill', state.revealAll ? '🙈 مخفی کردن پاسخ‌ها' : '👁 نمایش همه پاسخ‌ها');
      tog.type = 'button';
      tog.addEventListener('click', function(){ state.revealAll = !state.revealAll; render(); });
      listBar.appendChild(tog);
      root.appendChild(listBar);

      var listHost = el('div');
      root.appendChild(listHost);
      renderListItems(listHost);
      return;
    }

    if(!ids.length){
      var done = el('section', 'card result-box');
      done.appendChild(el('h2', 'result-title', state.filter === 'review' ? '🎉 همه کارت‌ها را بلدی!' : 'کارتی در این بخش نیست'));
      done.appendChild(el('p', 'hero-sub', 'می‌توانی همه کارت‌ها را دوباره مرور کنی یا وضعیت را ریست کنی.'));
      var acts = el('div', 'result-actions');
      var showAll = el('button', 'btn btn-primary', 'نمایش همه کارت‌ها');
      showAll.type = 'button';
      showAll.addEventListener('click', function(){ state.filter = 'all'; state.index = 0; state.flipped = false; render(); });
      acts.appendChild(showAll);
      done.appendChild(acts);
      root.appendChild(done);
      return;
    }

    var cardData = allCards[ids[state.index]];
    var st = state.status[cardData.id] || 'new';
    var stage = el('div', 'fc-stage');
    var fc = el('article', 'fc-card' + (state.flipped ? ' flipped' : ''));
    fc.setAttribute('tabindex', '0');
    fc.setAttribute('role', 'button');

    var top = el('div', 'fc-top');
    top.appendChild(el('span', 'badge acc', (state.flipped ? 'پشت کارت · پاسخ' : 'روی کارت · پرسش') + ' (' + fa(state.index + 1) + ' از ' + fa(ids.length) + ')'));
    if(st === 'known') top.appendChild(el('span', 'badge ok', '✅ بلدم'));
    else if(st === 'review') top.appendChild(el('span', 'badge warn', '🔁 نیاز به مرور'));
    fc.appendChild(top);

    var body = el('div', 'fc-body', state.flipped ? cardData.back : cardData.front);
    body.setAttribute('dir', 'auto');
    fc.appendChild(body);

    var foot = el('div');
    if(state.flipped && cardData.source){
      foot.appendChild(el('div', 'src-chip', '📍 منبع: ' + cardData.source));
    }
    foot.appendChild(el('div', 'fc-hint', state.flipped ? 'برای بازگشت به پرسش روی کارت بزنید' : 'برای دیدن پاسخ روی کارت بزنید یا کلید فاصله (Space) را فشار دهید'));
    fc.appendChild(foot);

    fc.addEventListener('click', function(){ state.flipped = !state.flipped; render(); });
    stage.appendChild(fc);
    root.appendChild(stage);

    var actions = el('div', 'fc-actions' + (state.flipped ? '' : ' single'));
    if(!state.flipped){
      var flipBtn = el('button', 'btn btn-primary', '👁 نمایش پاسخ');
      flipBtn.type = 'button';
      flipBtn.addEventListener('click', function(){ state.flipped = true; render(); });
      actions.appendChild(flipBtn);
    } else {
      var revBtn = el('button', 'btn btn-warn', '🔁 نیاز به مرور');
      revBtn.type = 'button';
      revBtn.addEventListener('click', function(){ markCard(cardData.id, 'review'); });
      var okBtn = el('button', 'btn btn-ok', '✅ بلدم');
      okBtn.type = 'button';
      okBtn.addEventListener('click', function(){ markCard(cardData.id, 'known'); });
      actions.appendChild(okBtn);
      actions.appendChild(revBtn);
    }
    root.appendChild(actions);

    var nav = el('div', 'nav-row');
    var prev = el('button', 'btn btn-secondary', '→ کارت قبلی');
    prev.type = 'button';
    prev.disabled = ids.length <= 1;
    prev.addEventListener('click', function(){
      state.index = (state.index - 1 + ids.length) % ids.length;
      state.flipped = false; render();
    });
    var next = el('button', 'btn btn-secondary', 'کارت بعدی ←');
    next.type = 'button';
    next.disabled = ids.length <= 1;
    next.addEventListener('click', function(){
      state.index = (state.index + 1) % ids.length;
      state.flipped = false; render();
    });
    nav.appendChild(prev);
    nav.appendChild(next);
    root.appendChild(nav);
  }

  function renderListItems(host){
    host.textContent = '';
    var q = state.query.trim().toLowerCase();
    var ids = visibleIds().filter(function(id){
      if(!q) return true;
      var card = allCards[id];
      return (card.front + ' ' + card.back + ' ' + card.source).toLowerCase().indexOf(q) !== -1;
    });
    if(!ids.length){
      host.appendChild(el('div', 'card empty', 'کارتی با این جستجو پیدا نشد.'));
      return;
    }
    ids.forEach(function(id, idx){
      var card = allCards[id];
      var row = el('article', 'list-item');
      var top = el('div', 'q-head');
      top.appendChild(el('span', 'badge', 'کارت ' + fa(idx + 1)));
      if(state.status[id] === 'known') top.appendChild(el('span', 'badge ok', '✅ بلدم'));
      else if(state.status[id] === 'review') top.appendChild(el('span', 'badge warn', '🔁 مرور'));
      row.appendChild(top);
      var qEl = el('div', 'list-q', card.front);
      qEl.setAttribute('dir', 'auto');
      row.appendChild(qEl);
      var aEl = el('div', 'list-a' + (state.revealAll ? '' : ' hidden'), card.back);
      aEl.setAttribute('dir', 'auto');
      row.appendChild(aEl);
      if(card.source){
        row.appendChild(el('div', 'src-chip', '📍 منبع: ' + card.source));
      }
      if(!state.revealAll){
        row.style.cursor = 'pointer';
        row.addEventListener('click', function(){ aEl.classList.toggle('hidden'); });
      }
      host.appendChild(row);
    });
  }

  document.addEventListener('keydown', function(e){
    if(state.mode !== 'study') return;
    if(document.activeElement && document.activeElement.tagName === 'INPUT') return;
    var ids = visibleIds();
    if(!ids.length) return;
    if(e.code === 'Space'){
      e.preventDefault();
      state.flipped = !state.flipped; render();
    } else if(e.key === 'ArrowLeft'){
      state.index = (state.index + 1) % ids.length; state.flipped = false; render();
    } else if(e.key === 'ArrowRight'){
      state.index = (state.index - 1 + ids.length) % ids.length; state.flipped = false; render();
    }
  });

  render();
})();
"""


def _render_shell(
    *,
    page_title: str,
    badge: str,
    subtitle: str,
    payload: dict[str, Any],
    script_body: str,
) -> bytes:
    font_css = _embedded_font_css()
    csp = learning_html_csp(script_body)
    safe_json = _safe_json_for_script(payload)
    doc = (
        "<!DOCTYPE html>\n"
        '<html lang="fa" dir="rtl">\n'
        "<head>\n"
        '<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width,initial-scale=1">\n'
        f'<meta http-equiv="Content-Security-Policy" content="{_esc(csp)}">\n'
        f"<title>{_esc(page_title)}</title>\n"
        f"<style>{font_css}{_SHARED_CSS}</style>\n"
        "</head>\n"
        "<body>\n"
        '<div class="shell">\n'
        '  <header class="topbar">\n'
        f'    <span class="brand">{_esc(badge)}</span>\n'
        '    <button type="button" id="theme-btn" class="icon-btn">🌙 حالت شب</button>\n'
        "  </header>\n"
        '  <section class="hero">\n'
        f'    <h1 class="hero-title" dir="auto">{_esc(page_title)}</h1>\n'
        f'    <p class="hero-sub" dir="auto">{_esc(subtitle)}</p>\n'
        '    <div class="stats" id="hero-stats"></div>\n'
        '    <div class="pbar"><div class="pfill" id="hero-pfill"></div></div>\n'
        "  </section>\n"
        '  <main id="app"></main>\n'
        '  <footer class="footer">ساخته‌شده با ربات گاماس · فایل تعاملی آفلاین</footer>\n'
        "</div>\n"
        f'<script type="application/json" id="{_DATA_ELEMENT_ID}">{safe_json}</script>\n'
        f"<script>{script_body}</script>\n"
        "</body>\n"
        "</html>\n"
    )
    return doc.encode("utf-8")


def render_quiz_html(
    title: str,
    questions: Sequence[Mapping[str, Any]],
    *,
    source_title: str = "",
) -> LearningHtmlArtifact:
    """Compile validated quiz questions into an offline interactive HTML file."""
    clean_title = _clean_text(title, limit=120) or "آزمون تعاملی"
    serialized: list[dict[str, Any]] = []
    for item in questions:
        stem = _clean_text(item.get("question") or item.get("stem"), limit=800)
        raw_opts = item.get("options") or ()
        options = [_clean_text(opt, limit=400) for opt in raw_opts]
        raw_correct = item.get("correct_index")
        if raw_correct is None:
            raw_correct = item.get("correct", 0)
        correct = int(raw_correct)
        explanation = _clean_text(item.get("explanation") or item.get("explain"), limit=1000)
        source = _clean_text(item.get("source") or item.get("source_hint"), limit=200)
        diff_val = str(item.get("difficulty") or "medium").strip().lower()
        difficulty = diff_val if diff_val in {"medium", "hard"} else "medium"
        if not stem or len(options) < 2 or not (0 <= correct < len(options)):
            continue
        serialized.append(
            {
                "stem": stem,
                "options": options,
                "correct": correct,
                "explanation": explanation,
                "source": source,
                "difficulty": difficulty,
            }
        )
    if not serialized:
        raise LearningHtmlRenderError("cannot render interactive quiz HTML without valid questions")

    clean_source = _clean_text(source_title, limit=120)
    count_fa = to_persian_digits(len(serialized))
    subtitle = (
        f"{count_fa} سؤال چهارگزینه‌ای با پاسخ تشریحی · منبع: {clean_source}"
        if clean_source and clean_source != clean_title
        else f"{count_fa} سؤال چهارگزینه‌ای با پاسخ تشریحی · قابل اجرا بدون اینترنت"
    )
    payload = {"kind": "quiz", "title": clean_title, "questions": serialized}
    content = _render_shell(
        page_title=clean_title,
        badge="🧪 گاماس · آزمون تعاملی",
        subtitle=subtitle,
        payload=payload,
        script_body=_QUIZ_SCRIPT,
    )
    return LearningHtmlArtifact(
        filename=_artifact_filename("quiz", clean_title),
        content=content,
        kind="quiz",
        item_count=len(serialized),
    )


def render_flashcards_html(
    title: str,
    cards: Sequence[Mapping[str, Any]],
    *,
    source_title: str = "",
) -> LearningHtmlArtifact:
    """Compile validated flashcards into an offline interactive HTML file."""
    clean_title = _clean_text(title, limit=120) or "فلش‌کارت تعاملی"
    serialized: list[dict[str, str]] = []
    for item in cards:
        front = _clean_text(item.get("front"), limit=800)
        back = _clean_text(item.get("back"), limit=1200)
        source = _clean_text(item.get("source") or item.get("source_hint"), limit=200)
        if not front or not back:
            continue
        serialized.append({"front": front, "back": back, "source": source})
    if not serialized:
        raise LearningHtmlRenderError("cannot render interactive flashcards HTML without valid cards")

    clean_source = _clean_text(source_title, limit=120)
    count_fa = to_persian_digits(len(serialized))
    subtitle = (
        f"{count_fa} فلش‌کارت تعاملی با حالت مرور و جستجو · منبع: {clean_source}"
        if clean_source and clean_source != clean_title
        else f"{count_fa} فلش‌کارت تعاملی با حالت مطالعه و جستجو · قابل اجرا بدون اینترنت"
    )
    payload = {"kind": "flashcards", "title": clean_title, "cards": serialized}
    content = _render_shell(
        page_title=clean_title,
        badge="🃏 گاماس · فلش‌کارت تعاملی",
        subtitle=subtitle,
        payload=payload,
        script_body=_FLASHCARDS_SCRIPT,
    )
    return LearningHtmlArtifact(
        filename=_artifact_filename("flashcards", clean_title),
        content=content,
        kind="flashcards",
        item_count=len(serialized),
    )
