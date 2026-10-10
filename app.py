#!/usr/bin/env python3
"""
Patente B quiz — local Flask app for browser (desktop, Termux, Pydroid).

UI chrome is always Italian. Language only affects question text:
  Italiano → Italian only
  Français / Deutsch → Italian + translation

Run:  python app.py
Open: http://127.0.0.1:5000
"""

from __future__ import annotations

import gc
import json
import logging
import os
import random
import secrets
import sys
import threading
import time
import traceback
from pathlib import Path

try:
    from flask import Flask, jsonify, request, send_from_directory, session
except ImportError:
    print("Flask is required. Install with: pip install flask")
    sys.exit(1)

# Dump native crashes to stderr when available (handy on mobile interpreters).
try:
    import faulthandler

    faulthandler.enable()
except Exception:
    pass

log = logging.getLogger("patente")

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
MEDIA_DIR = BASE_DIR / "media"

# code → display name and optional translation file (None = Italian only)
LANGUAGES = {
    "it": {"name": "Italiano", "file": None},
    "fr": {"name": "Français", "file": "questions_fr.json"},
    "de": {"name": "Deutsch", "file": "questions_de.json"},
}

# Fixed Italian UI strings (questions may still be bilingual)
UI = {
    "title": "Quiz Patente B",
    "select_lang": "Scegli la lingua",
    "streak": "Serie",
    "mistakes": "Errori (30)",
    "true": "V",
    "false": "F",
    "true_label": "Vero",
    "false_label": "Falso",
    "correct": "Corretto!",
    "wrong": "Sbagliato",
    "continue": "Continua",
    "change_lang": "Cambia lingua",
    "no_image": "Nessuna immagine",
    "loading": "Caricamento…",
    "error_load": "Impossibile caricare le domande.",
    "retry": "Riprova",
    "prev": "Precedente",
    "next": "Successiva",
    "review": "Rivedi",
    "current": "Corrente",
}

ROLLING_WINDOW = 30  # mistakes counter window
SEEN_WINDOW = 50  # avoid re-drawing these recent question ids
_PICK_TRIES = 48  # rejection samples before a full filter pass
HISTORY_MAX = 200  # compact [id, answer] pairs kept per session

# ---------------------------------------------------------------------------
# Player state: browser cookie holds only an opaque sid; quiz data is in RAM.
# ---------------------------------------------------------------------------
_state_lock = threading.RLock()
_STATE: dict[str, dict] = {}
_STATE_TTL_SEC = 60 * 60 * 2  # idle session expiry
_STATE_MAX = 12  # max concurrent player slots

# Shared defaults so 7k rows do not each hold a unique explanation string
_EXPLAIN_V = "La risposta corretta è Vero."
_EXPLAIN_F = "La risposta corretta è Falso."


def _new_player_state(lang: str | None = None) -> dict:
    return {
        "lang": lang,
        "streak": 0,
        "seen": [],  # recent ids, length ≤ SEEN_WINDOW
        "recent": [],  # last ROLLING_WINDOW correct/incorrect flags
        "history": [],  # compact [qid, user_answer] for back-navigation
        "last_answer": None,  # last graded payload (idempotent retries)
        "touched": time.time(),
    }


def _append_history(st: dict, qid, user_answer: str) -> None:
    """Store only question id + user's V/F (rehydrate text from the bank on review)."""
    hist = list(st.get("history") or [])
    hist.append([int(qid), user_answer])
    if len(hist) > HISTORY_MAX:
        hist = hist[-HISTORY_MAX:]
    st["history"] = hist


def _purge_stale_states() -> None:
    now = time.time()
    dead = [sid for sid, st in _STATE.items() if now - st.get("touched", 0) > _STATE_TTL_SEC]
    for sid in dead:
        _STATE.pop(sid, None)
    # Evict least-recently used if over the hard cap
    overflow = len(_STATE) - _STATE_MAX
    if overflow > 0:
        oldest = sorted(_STATE.items(), key=lambda kv: kv[1].get("touched", 0))
        for sid, _ in oldest[:overflow]:
            _STATE.pop(sid, None)


def get_player_state(create: bool = True) -> dict | None:
    """In-memory state for this browser session (cookie stores sid only)."""
    with _state_lock:
        _purge_stale_states()
        sid = session.get("sid")
        if sid and sid in _STATE:
            st = _STATE[sid]
            st["touched"] = time.time()
            return st
        if not create:
            return None
        sid = secrets.token_hex(16)
        session["sid"] = sid
        session.permanent = True
        st = _new_player_state()
        _STATE[sid] = st
        return st


def _stats_from(st: dict) -> dict:
    recent = list(st.get("recent") or [])[-ROLLING_WINDOW:]
    return {
        "streak": int(st.get("streak") or 0),
        "mistakes_30": sum(1 for ok in recent if not ok),
        "answered_30": len(recent),
        "history_len": len(st.get("history") or []),
    }


def _public_question(q: dict, lang: str, st: dict) -> dict:
    return {
        "id": q["id"],
        "question_it": q["question_it"],
        "question_tr": q["question_tr"],
        "image": q["image"],
        "lang": lang,
        **_stats_from(st),
    }


def _pick_question(st: dict, questions: list) -> dict:
    """Random question, skipping ids in the recent SEEN_WINDOW (rejection sampling)."""
    n = len(questions)
    if n == 0:
        raise ValueError("empty question bank")

    with _state_lock:
        seen = st.get("seen") or []
        if len(seen) > SEEN_WINDOW:
            seen = seen[-SEEN_WINDOW:]
        seen_set = set(seen) if seen else set()

        q = None
        if not seen_set:
            q = questions[random.randrange(n)]
        else:
            for _ in range(_PICK_TRIES):
                cand = questions[random.randrange(n)]
                if cand["id"] not in seen_set:
                    q = cand
                    break
            if q is None:
                # Fallback: one linear filter, or reset seen if exhausted
                available = [item for item in questions if item["id"] not in seen_set]
                if not available:
                    seen = []
                    seen_set = set()
                    q = questions[random.randrange(n)]
                else:
                    q = random.choice(available)

        seen = list(seen)
        seen.append(q["id"])
        st["seen"] = seen[-SEEN_WINDOW:]
        st["touched"] = time.time()
        return q


# Active language bank only (raw JSON is not kept after build)
_questions_cache: dict[str, list] = {}
_id_index: dict[str, dict] = {}  # lang → {id: question}
_load_lock = threading.Lock()


def _load_raw_file(filename: str) -> list:
    """Load a JSON list from disk (not cached long-term)."""
    path = DATA_DIR / filename
    if not path.exists():
        raise FileNotFoundError(f"Missing data file: {path}")
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def load_questions(lang: str) -> list:
    """
    Question bank for one language mode.

    Disk schema:
      questions_it.json → id, question, answer, image
      questions_fr/de.json → id, question  (translation only)

    In-memory item: id, answer, image, question_it, question_tr, explanation.
    Only one language is kept in memory; switching drops the previous bank.
    """
    if lang in _questions_cache:
        return _questions_cache[lang]
    if lang not in LANGUAGES:
        raise ValueError(f"Unknown language: {lang}")

    with _load_lock:
        if lang in _questions_cache:
            return _questions_cache[lang]

        log.info("Loading question bank for lang=%s", lang)
        italian = _load_raw_file("questions_it.json")
        tr_map: dict[int, str] = {}
        tr_file = LANGUAGES[lang]["file"]
        if tr_file:
            for item in _load_raw_file(tr_file):
                tr_map[int(item["id"])] = item.get("question") or ""

        out = []
        idx: dict = {}
        for q in italian:
            qid = int(q["id"])
            it_text = q.get("question") or ""
            ans = q["answer"]
            item = {
                "id": qid,
                "answer": ans,
                "image": q.get("image"),
                "question_it": it_text,
                "question_tr": tr_map.get(qid) if tr_file else None,
                "explanation": _EXPLAIN_V if ans == "V" else _EXPLAIN_F,
            }
            out.append(item)
            idx[qid] = item

        # Keep a single bank resident
        for other in list(_questions_cache.keys()):
            if other != lang:
                _questions_cache.pop(other, None)
                _id_index.pop(other, None)

        del italian
        del tr_map

        _questions_cache[lang] = out
        _id_index[lang] = idx
        gc.collect()
        log.info("Loaded %s questions for lang=%s", len(out), lang)
        return out


app = Flask(__name__)
app.secret_key = os.environ.get("PATENTE_SECRET", "patente-b-quiz-termux")
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["PERMANENT_SESSION_LIFETIME"] = 60 * 60 * 12


def _find_question(lang: str, qid) -> dict | None:
    """Look up a question by id (JSON numbers become int at load)."""
    if qid is None:
        return None
    idx = _id_index.get(lang) or {}
    if not idx and lang in _questions_cache:
        idx = {q["id"]: q for q in _questions_cache[lang]}
        _id_index[lang] = idx
    try:
        return idx.get(int(qid))
    except (TypeError, ValueError):
        return None


@app.route("/media/<path:filename>")
def media(filename: str):
    return send_from_directory(MEDIA_DIR, filename)


@app.route("/api/languages")
def api_languages():
    return jsonify(
        {
            code: {"name": info["name"]}
            for code, info in LANGUAGES.items()
        }
    )


@app.route("/api/start", methods=["POST"])
def api_start():
    body = request.get_json(force=True, silent=True) or {}
    lang = body.get("lang", "it")
    if lang not in LANGUAGES:
        return jsonify({"error": "invalid language"}), 400
    try:
        questions = load_questions(lang)
    except FileNotFoundError as e:
        return jsonify({"error": str(e)}), 404

    st = get_player_state(create=True)
    st["lang"] = lang
    st["streak"] = 0
    st["seen"] = []
    st["recent"] = []
    st["history"] = []
    st["last_answer"] = None
    st["touched"] = time.time()

    return jsonify(
        {
            "lang": lang,
            "total": len(questions),
            **_stats_from(st),
        }
    )


@app.route("/api/question", methods=["GET"])
def api_question():
    st = get_player_state(create=True)
    # Server lang first; ?lang= is a fallback if the session was lost
    lang = st.get("lang") or request.args.get("lang") or "it"
    if lang not in LANGUAGES:
        return jsonify({"error": "select a language first", "code": "no_lang"}), 400
    if not st.get("lang"):
        st["lang"] = lang

    try:
        questions = load_questions(lang)
    except FileNotFoundError as e:
        return jsonify({"error": str(e)}), 404
    except Exception as e:
        return jsonify({"error": f"load failed: {e}"}), 500

    q = _pick_question(st, questions)
    return jsonify(_public_question(q, lang, st))


@app.route("/api/answer", methods=["POST"])
def api_answer():
    """
    Grade an answer and return the next question in the same response.

    Successful responses always include a usable `next`. Pick failures are
    HTTP errors (never `"next": null`). Retries of the same question id are
    idempotent and do not double-count streak or mistakes.
    """
    body = request.get_json(force=True, silent=True) or {}
    st = get_player_state(create=True)
    lang = st.get("lang") or body.get("lang") or "it"
    qid = body.get("id")
    user_answer = (body.get("answer") or "").strip().upper()
    if user_answer not in ("V", "F"):
        return jsonify({"error": "answer must be V or F"}), 400
    if lang not in LANGUAGES:
        return jsonify({"error": "select a language first", "code": "no_lang"}), 400
    if not st.get("lang"):
        st["lang"] = lang

    try:
        questions = load_questions(lang)
    except FileNotFoundError as e:
        return jsonify({"error": str(e)}), 404

    if not questions:
        return jsonify({"error": "question bank empty", "code": "no_next"}), 500

    q = _find_question(lang, qid)
    if not q:
        return jsonify({"error": "question not found", "code": "not_found"}), 404

    correct = user_answer == q["answer"]

    # Idempotent retry, pick next, then score — all under one lock
    with _state_lock:
        last = st.get("last_answer") or {}
        try:
            same_qid = int(last.get("qid")) == int(q["id"])
        except (TypeError, ValueError):
            same_qid = str(last.get("qid")) == str(q["id"])
        if same_qid and last.get("payload") and (last["payload"] or {}).get("next"):
            return jsonify(last["payload"])

        # Pick before scoring so a failed pick does not change streak
        try:
            nxt = _pick_question(st, questions)
        except Exception as e:
            return jsonify(
                {"error": f"failed to pick next question: {e}", "code": "no_next"}
            ), 500

        if not nxt or nxt.get("id") is None or not nxt.get("question_it"):
            return jsonify(
                {"error": "next question incomplete", "code": "no_next"}
            ), 500

        if correct:
            st["streak"] = int(st.get("streak") or 0) + 1
        else:
            st["streak"] = 0

        recent = list(st.get("recent") or [])[-ROLLING_WINDOW:]
        recent.append(bool(correct))
        st["recent"] = recent[-ROLLING_WINDOW:]
        _append_history(st, q["id"], user_answer)
        st["touched"] = time.time()

        # Public next after scoring so embedded stats match this answer
        next_payload = _public_question(nxt, lang, st)

        payload = {
            "correct": correct,
            "correct_answer": q["answer"],
            "explanation": q.get("explanation") or "",
            "next": next_payload,
            **_stats_from(st),
        }
        st["last_answer"] = {"qid": q["id"], "payload": payload}

    return jsonify(payload)


@app.route("/api/review", methods=["GET"])
def api_review():
    """
    Rehydrate a past answer from compact history: only id + user answer are stored.
    Question text/image/explanation come from the live bank. Does not score or advance.
    """
    st = get_player_state(create=True)
    lang = st.get("lang") or request.args.get("lang") or "it"
    if lang not in LANGUAGES:
        return jsonify({"error": "select a language first", "code": "no_lang"}), 400
    if not st.get("lang"):
        st["lang"] = lang

    hist = st.get("history") or []
    if not hist:
        return jsonify({"error": "no history", "code": "empty_history"}), 404

    try:
        idx = int(request.args.get("i", len(hist) - 1))
    except (TypeError, ValueError):
        return jsonify({"error": "invalid index"}), 400
    if idx < 0 or idx >= len(hist):
        return jsonify({"error": "index out of range", "code": "bad_index"}), 400

    try:
        load_questions(lang)
    except FileNotFoundError as e:
        return jsonify({"error": str(e)}), 404

    entry = hist[idx]
    try:
        qid, user_answer = entry[0], str(entry[1]).strip().upper()
    except (TypeError, IndexError, ValueError):
        return jsonify({"error": "corrupt history entry"}), 500

    q = _find_question(lang, qid)
    if not q:
        return jsonify({"error": "question not found", "code": "not_found"}), 404

    if user_answer not in ("V", "F"):
        return jsonify({"error": "corrupt history answer"}), 500

    correct = user_answer == q["answer"]
    st["touched"] = time.time()
    return jsonify(
        {
            **_public_question(q, lang, st),
            "review": True,
            "user_answer": user_answer,
            "correct": correct,
            "correct_answer": q["answer"],
            "explanation": (q.get("explanation") or "") if not correct else "",
            "history_index": idx,
            "history_len": len(hist),
            "can_prev": idx > 0,
            # Client treats "next" as either later history or return to live question
            "can_next": True,
        }
    )


@app.route("/")
def index():
    # Single source of truth: inject Python UI into the page (no API ui field).
    return INDEX_HTML.replace("__UI_JSON__", json.dumps(UI, ensure_ascii=False))


INDEX_HTML = r"""<!DOCTYPE html>
<html lang="it">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1, user-scalable=no" />
  <title>Quiz Patente B</title>
  <style>
    :root {
      --bg: #0f1419;
      --card: #1a2332;
      --text: #e7ecf3;
      --muted: #8b9bb4;
      --accent: #3b82f6;
      --good: #22c55e;
      --bad: #ef4444;
      --btn: #243044;
      --border: #2d3a4f;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      font-family: system-ui, -apple-system, Segoe UI, Roboto, Arial, sans-serif;
      background: var(--bg);
      color: var(--text);
      min-height: 100vh;
      line-height: 1.45;
    }
    .wrap {
      max-width: 560px;
      margin: 0 auto;
      padding: 16px;
      padding-bottom: 32px;
    }
    h1 {
      font-size: 1.35rem;
      margin: 8px 0 16px;
      text-align: center;
      font-weight: 700;
    }
    .card {
      background: var(--card);
      border: 1px solid var(--border);
      border-radius: 16px;
      padding: 18px;
      box-shadow: 0 8px 24px rgba(0,0,0,.25);
    }
    .hidden { display: none !important; }
    .lang-grid { display: grid; gap: 12px; }
    button {
      font: inherit;
      border: none;
      border-radius: 12px;
      padding: 14px 18px;
      cursor: pointer;
      background: var(--btn);
      color: var(--text);
      border: 1px solid var(--border);
      transition: transform .05s ease, background .15s ease;
    }
    button:active { transform: scale(0.98); }
    button.primary {
      background: var(--accent);
      border-color: transparent;
      color: white;
      font-weight: 600;
    }
    button.v {
      background: #166534;
      border-color: #22c55e55;
      color: #ecfdf5;
      font-size: 1.5rem;
      font-weight: 800;
      min-height: 64px;
    }
    button.f {
      background: #7f1d1d;
      border-color: #ef444455;
      color: #fef2f2;
      font-size: 1.5rem;
      font-weight: 800;
      min-height: 64px;
    }
    button:disabled { opacity: 0.5; cursor: not-allowed; }
    .topbar {
      display: flex;
      justify-content: space-between;
      align-items: center;
      margin-bottom: 12px;
      gap: 8px;
      flex-wrap: wrap;
    }
    .stats {
      display: flex;
      gap: 8px;
      flex-wrap: wrap;
      justify-content: flex-end;
    }
    .stat {
      background: #1e3a5f;
      color: #93c5fd;
      padding: 6px 12px;
      border-radius: 999px;
      font-weight: 700;
      font-size: 0.9rem;
      white-space: nowrap;
    }
    .stat.mistakes {
      background: #3f1d2e;
      color: #f9a8d4;
    }
    .linkish {
      background: transparent;
      border: none;
      color: var(--muted);
      padding: 6px 8px;
      font-size: 0.9rem;
      text-decoration: underline;
    }
    .qtext {
      margin: 12px 0 16px;
      text-align: center;
    }
    .q-it {
      font-size: 1.1rem;
      margin: 0 0 8px;
    }
    .q-tr {
      font-size: 1.0rem;
      margin: 0;
      color: #b8c5d9;
      font-style: italic;
      border-top: 1px solid var(--border);
      padding-top: 10px;
    }
    .imgbox {
      display: flex;
      justify-content: center;
      align-items: center;
      min-height: 40px;
      margin: 8px 0 16px;
      background: #0b1018;
      border-radius: 12px;
      overflow: hidden;
      border: 1px solid var(--border);
    }
    .imgbox img {
      max-width: 100%;
      max-height: 320px;
      height: auto;
      display: block;
      object-fit: contain;
    }
    .imgbox .placeholder {
      color: var(--muted);
      padding: 24px;
      font-size: 0.9rem;
    }
    .answers {
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 12px;
    }
    .feedback {
      margin-top: 16px;
      padding: 14px;
      border-radius: 12px;
      border: 1px solid var(--border);
    }
    .feedback.ok {
      background: #14532d44;
      border-color: #22c55e66;
    }
    .feedback.bad {
      background: #7f1d1d44;
      border-color: #ef444466;
    }
    .feedback h3 { margin: 0 0 8px; font-size: 1.05rem; }
    .feedback p { margin: 0 0 12px; color: var(--text); }
    .muted { color: var(--muted); text-align: center; }
    .sub { color: var(--muted); font-size: 0.9rem; text-align: center; margin-bottom: 16px; }
    .nav-row {
      display: flex;
      align-items: center;
      gap: 8px;
      margin-bottom: 12px;
    }
    .nav-row button {
      flex: 1;
      padding: 10px 12px;
      font-size: 0.9rem;
      min-height: 44px;
    }
    .nav-pos {
      flex: 0 0 auto;
      min-width: 5.5rem;
      text-align: center;
      color: var(--muted);
      font-size: 0.85rem;
      font-weight: 600;
    }
    button.v.picked, button.f.picked {
      box-shadow: 0 0 0 2px #93c5fd;
    }
    button.v.picked.ok-pick, button.f.picked.ok-pick {
      box-shadow: 0 0 0 2px var(--good);
    }
    button.v.picked.bad-pick, button.f.picked.bad-pick {
      box-shadow: 0 0 0 2px var(--bad);
    }
  </style>
</head>
<body>
  <div class="wrap">
    <h1 id="app-title">Quiz Patente B</h1>

    <div id="screen-lang" class="card">
      <p class="sub" id="lang-prompt">Scegli la lingua</p>
      <div class="lang-grid" id="lang-buttons"></div>
    </div>

    <div id="screen-quiz" class="card hidden">
      <div class="topbar">
        <button type="button" class="linkish" id="btn-change-lang">Cambia lingua</button>
        <div class="stats">
          <div class="stat" id="streak">Serie: 0</div>
          <div class="stat mistakes" id="mistakes">Errori (30): 0</div>
        </div>
      </div>
      <div class="nav-row">
        <button type="button" id="btn-prev" disabled>Precedente</button>
        <span class="nav-pos" id="nav-pos"></span>
        <button type="button" id="btn-next" disabled>Successiva</button>
      </div>
      <div class="imgbox" id="imgbox">
        <span class="placeholder" id="img-placeholder">Nessuna immagine</span>
      </div>
      <div class="qtext" id="question-box">
        <p class="q-it" id="question-it">…</p>
        <p class="q-tr hidden" id="question-tr"></p>
      </div>
      <div class="answers" id="answers">
        <button type="button" class="v" id="btn-v" title="Vero">V</button>
        <button type="button" class="f" id="btn-f" title="Falso">F</button>
      </div>
      <div id="feedback" class="feedback hidden"></div>
    </div>

    <p class="muted" id="status" style="margin-top:12px"></p>
  </div>

  <script>
    // Injected from Python UI dict (single source of truth)
    const UI = __UI_JSON__;

    let currentId = null;
    let currentLang = null;
    let busy = false;
    let loadGen = 0; // bump to ignore stale async results
    let pendingNext = null; // next question from the last /api/answer
    let lastAnswerReq = null; // last POST body for idempotent recovery
    // History nav: server stores only [id, answer]; client stashes the live question
    let historyLen = 0;
    let reviewIndex = null; // null = live unanswered question
    let liveQuestion = null; // current unanswered question payload

    const el = (id) => document.getElementById(id);
    const setText = (id, text) => {
      const node = el(id);
      if (node) node.textContent = text;
    };
    const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

    function isUsableQuestion(q) {
      return !!(q && q.id != null && q.question_it);
    }

    function isReviewMode() {
      return reviewIndex != null;
    }

    function clearAnswerHighlight() {
      const btnV = el("btn-v");
      const btnF = el("btn-f");
      if (btnV) btnV.classList.remove("picked", "ok-pick", "bad-pick");
      if (btnF) btnF.classList.remove("picked", "ok-pick", "bad-pick");
    }

    function markUserAnswer(userAns, correct) {
      clearAnswerHighlight();
      const btn = userAns === "V" ? el("btn-v") : el("btn-f");
      if (!btn) return;
      btn.classList.add("picked");
      btn.classList.add(correct ? "ok-pick" : "bad-pick");
    }

    function updateNavUi() {
      const prev = el("btn-prev");
      const next = el("btn-next");
      const pos = el("nav-pos");
      if (prev) {
        prev.textContent = UI.prev || "Precedente";
        prev.disabled = busy || historyLen <= 0 || (isReviewMode() && reviewIndex <= 0);
      }
      if (next) {
        next.textContent = UI.next || "Successiva";
        // Next is for leaving review toward later history or live
        next.disabled = busy || !isReviewMode();
      }
      if (pos) {
        if (isReviewMode()) {
          const label = UI.review || "Rivedi";
          pos.textContent = `${label} ${reviewIndex + 1}/${historyLen}`;
        } else if (historyLen > 0) {
          pos.textContent = UI.current || "Corrente";
        } else {
          pos.textContent = "";
        }
      }
    }

    function showReviewFeedback(data) {
      const fb = el("feedback");
      if (!fb) return;
      if (data.correct) {
        fb.className = "feedback ok";
        fb.innerHTML = `<h3>${escapeHtml(UI.correct)}</h3>`;
      } else {
        fb.className = "feedback bad";
        fb.innerHTML = `
          <h3>${escapeHtml(UI.wrong)}</h3>
          <p>${escapeHtml(data.explanation || "")}</p>
        `;
      }
      fb.classList.remove("hidden");
      markUserAnswer(data.user_answer, data.correct);
    }

    function hideFeedback() {
      const fb = el("feedback");
      if (fb) {
        fb.classList.add("hidden");
        fb.innerHTML = "";
      }
      clearAnswerHighlight();
    }

    async function api(path, opts, retries) {
      // Retry network / 5xx blips common on mobile localhost
      const maxTries = retries == null ? 6 : retries;
      let lastErr = null;
      for (let attempt = 1; attempt <= maxTries; attempt++) {
        try {
          const controller = typeof AbortController !== "undefined"
            ? new AbortController()
            : null;
          const timer = controller
            ? setTimeout(() => controller.abort(), 15000)
            : null;
          try {
            const res = await fetch(path, {
              headers: { "Content-Type": "application/json" },
              credentials: "same-origin",
              cache: "no-store",
              signal: controller ? controller.signal : undefined,
              ...opts,
            });
            const text = await res.text();
            let data = {};
            try { data = text ? JSON.parse(text) : {}; } catch (_) {
              if (!res.ok) throw Object;
              data = {};
            }
            if (!res.ok) {
              const err = new Error(data.error || res.statusText || "request failed");
              err.code = data.code;
              err.status = res.status;
              throw err;
            }
            return data;
          } finally {
            if (timer) clearTimeout(timer);
          }
        } catch (e) {
          lastErr = e;
          // No retry on most 4xx (except 429)
          if (e.status && e.status >= 400 && e.status < 500 && e.status !== 429) {
            throw e;
          }
          if (attempt < maxTries) {
            await sleep(Math.min(2000, 250 * Math.pow(1.6, attempt - 1)));
          }
        }
      }
      const msg = lastErr && lastErr.message ? lastErr.message : "request failed";
      if (/failed to fetch|abort/i.test(msg)) {
        throw new Error("Connessione interrotta — riprova");
      }
      throw lastErr || new Error("request failed");
    }

    function show(screen) {
      const lang = el("screen-lang");
      const quiz = el("screen-quiz");
      if (lang) lang.classList.toggle("hidden", screen !== "lang");
      if (quiz) quiz.classList.toggle("hidden", screen !== "quiz");
    }

    function setStats(data) {
      const streak = data.streak != null ? data.streak : 0;
      const mistakes = data.mistakes_30 != null ? data.mistakes_30 : 0;
      setText("streak", `${UI.streak}: ${streak}`);
      setText("mistakes", `${UI.mistakes}: ${mistakes}`);
      if (data.history_len != null) historyLen = data.history_len;
      updateNavUi();
    }

    function showNoImage(box) {
      box.innerHTML = "";
      const span = document.createElement("span");
      span.className = "placeholder";
      span.id = "img-placeholder";
      span.textContent = UI.no_image;
      box.appendChild(span);
    }

    function renderQuestionText(q) {
      setText("question-it", q.question_it || "");
      const tr = el("question-tr");
      if (!tr) return;
      if (q.question_tr && q.question_tr !== q.question_it) {
        tr.textContent = q.question_tr;
        tr.classList.remove("hidden");
      } else {
        tr.textContent = "";
        tr.classList.add("hidden");
      }
    }

    function showLoadError(msg, onRetry) {
      const fb = el("feedback");
      if (fb) {
        fb.className = "feedback bad";
        fb.innerHTML = `
          <h3>${escapeHtml(UI.error_load)}</h3>
          <p>${escapeHtml(msg || "")}</p>
          <button type="button" class="primary" id="btn-retry">${escapeHtml(UI.retry || "Riprova")}</button>
        `;
        fb.classList.remove("hidden");
        const btn = el("btn-retry");
        if (btn) {
          btn.onclick = typeof onRetry === "function"
            ? onRetry
            : () => loadQuestion({ allowNetwork: true });
        }
      }
      setText("status", msg || UI.error_load);
    }

    async function initLangScreen() {
      const langs = await api("/api/languages");
      const box = el("lang-buttons");
      box.innerHTML = "";
      for (const [code, info] of Object.entries(langs)) {
        const b = document.createElement("button");
        b.className = "primary";
        b.textContent = info.name;
        b.onclick = () => startLang(code);
        box.appendChild(b);
      }
      setText("app-title", UI.title);
      setText("lang-prompt", UI.select_lang);
      show("lang");
    }

    async function startLang(code) {
      setText("status", UI.loading);
      pendingNext = null;
      lastAnswerReq = null;
      historyLen = 0;
      reviewIndex = null;
      liveQuestion = null;
      try {
        const data = await api("/api/start", {
          method: "POST",
          body: JSON.stringify({ lang: code }),
        });
        currentLang = code;
        setStats(data);
        setText("status", "");
        show("quiz");
        await loadQuestion({ allowNetwork: true });
      } catch (e) {
        setText("status", UI.error_load + " " + e.message);
      }
    }

    function setAnswerEnabled(on) {
      const btnV = el("btn-v");
      const btnF = el("btn-f");
      // Never answer while reviewing past questions
      const enable = on && !isReviewMode();
      if (btnV) btnV.disabled = !enable;
      if (btnF) btnF.disabled = !enable;
    }

    function applyQuestion(q, opts) {
      if (!isUsableQuestion(q)) {
        throw new Error("risposta vuota dal server");
      }
      const asLive = !opts || opts.asLive !== false;
      currentId = q.id;
      if (q.lang) currentLang = q.lang;
      renderQuestionText(q);
      setStats(q);
      const box = el("imgbox");
      if (box) {
        box.innerHTML = "";
        if (q.image) {
          const img = document.createElement("img");
          img.src = "/media/" + encodeURIComponent(q.image);
          img.alt = "quiz";
          img.onerror = () => showNoImage(box);
          box.appendChild(img);
        } else {
          showNoImage(box);
        }
      }
      if (asLive) {
        // Stash live payload so Prev→Next can restore without a new draw
        liveQuestion = {
          id: q.id,
          question_it: q.question_it,
          question_tr: q.question_tr,
          image: q.image,
          lang: q.lang || currentLang,
          streak: q.streak,
          mistakes_30: q.mistakes_30,
          answered_30: q.answered_30,
          history_len: q.history_len != null ? q.history_len : historyLen,
        };
        reviewIndex = null;
        clearAnswerHighlight();
      }
      updateNavUi();
    }

    // After an answer: show pendingNext only (no GET /api/question).
    // Cache is cleared only after a successful paint.
    async function advanceFromCache() {
      const gen = ++loadGen;
      busy = true;
      setAnswerEnabled(false);
      hideFeedback();
      setText("status", UI.loading);
      updateNavUi();
      try {
        if (!isUsableQuestion(pendingNext)) {
          // Idempotent re-POST recovers next without a separate GET
          if (lastAnswerReq) {
            const r = await api("/api/answer", {
              method: "POST",
              body: JSON.stringify(lastAnswerReq),
            }, 6);
            if (isUsableQuestion(r.next)) {
              pendingNext = r.next;
              setStats(r);
            }
          }
        }
        if (!isUsableQuestion(pendingNext)) {
          throw new Error(
            "Prossima domanda non disponibile — tocca Riprova (senza ricaricare la domanda via rete)"
          );
        }
        if (gen !== loadGen) return;
        applyQuestion(pendingNext, { asLive: true });
        pendingNext = null;
        setText("status", "");
        setAnswerEnabled(true);
      } catch (e) {
        if (gen !== loadGen) return;
        showLoadError(e.message, () => {
          advanceFromCache().catch((err) =>
            showLoadError(err.message, () => advanceFromCache())
          );
        });
        setAnswerEnabled(false);
      } finally {
        if (gen === loadGen) {
          busy = false;
          updateNavUi();
        }
      }
    }

    // First question / language change (may use the network)
    async function loadQuestion(opts) {
      const allowNetwork = !opts || opts.allowNetwork !== false;
      const gen = ++loadGen;
      busy = true;
      setAnswerEnabled(false);
      hideFeedback();
      setText("status", UI.loading);
      updateNavUi();
      try {
        let q = null;
        if (isUsableQuestion(pendingNext)) {
          q = pendingNext;
        } else if (allowNetwork) {
          const qpath = currentLang
            ? "/api/question?lang=" + encodeURIComponent(currentLang)
            : "/api/question";
          try {
            q = await api(qpath);
          } catch (e) {
            if (currentLang && (e.code === "no_lang" || /language/i.test(e.message || ""))) {
              await api("/api/start", {
                method: "POST",
                body: JSON.stringify({ lang: currentLang }),
              }, 3);
              q = await api(qpath);
            } else {
              throw e;
            }
          }
        } else {
          throw new Error("Nessuna domanda in cache");
        }
        if (gen !== loadGen) return;
        applyQuestion(q, { asLive: true });
        if (pendingNext && q && pendingNext.id === q.id) {
          pendingNext = null;
        }
        setText("status", "");
        setAnswerEnabled(true);
      } catch (e) {
        if (gen !== loadGen) return;
        showLoadError(e.message, () => loadQuestion({ allowNetwork: true }));
        setAnswerEnabled(false);
      } finally {
        if (gen === loadGen) {
          busy = false;
          updateNavUi();
        }
      }
    }

    async function showHistoryAt(index) {
      if (index < 0 || index >= historyLen) return;
      const gen = ++loadGen;
      busy = true;
      setAnswerEnabled(false);
      setText("status", UI.loading);
      updateNavUi();
      try {
        const data = await api("/api/review?i=" + encodeURIComponent(index));
        if (gen !== loadGen) return;
        reviewIndex = data.history_index != null ? data.history_index : index;
        if (data.history_len != null) historyLen = data.history_len;
        applyQuestion(data, { asLive: false });
        showReviewFeedback(data);
        setText("status", "");
        setAnswerEnabled(false);
      } catch (e) {
        if (gen !== loadGen) return;
        showLoadError(e.message, () => showHistoryAt(index));
      } finally {
        if (gen === loadGen) {
          busy = false;
          updateNavUi();
        }
      }
    }

    function restoreLiveQuestion() {
      if (!isUsableQuestion(liveQuestion)) {
        // Fall back to network if stash was lost
        reviewIndex = null;
        return loadQuestion({ allowNetwork: true });
      }
      const gen = ++loadGen;
      busy = true;
      hideFeedback();
      setText("status", "");
      try {
        reviewIndex = null;
        applyQuestion(liveQuestion, { asLive: true });
        setAnswerEnabled(true);
      } finally {
        if (gen === loadGen) {
          busy = false;
          updateNavUi();
        }
      }
      return Promise.resolve();
    }

    async function goPrev() {
      if (busy || historyLen <= 0) return;
      if (!isReviewMode()) {
        // Leaving live → last answered
        await showHistoryAt(historyLen - 1);
        return;
      }
      if (reviewIndex > 0) await showHistoryAt(reviewIndex - 1);
    }

    async function goNext() {
      if (busy || !isReviewMode()) return;
      if (reviewIndex < historyLen - 1) {
        await showHistoryAt(reviewIndex + 1);
        return;
      }
      // Past the last answered → back to the live current question
      await restoreLiveQuestion();
    }

    async function answer(ans) {
      if (busy || currentId == null || isReviewMode()) return;
      busy = true;
      setAnswerEnabled(false);
      setText("status", "");
      updateNavUi();
      const answeredId = currentId;
      const reqBody = {
        id: answeredId,
        answer: ans,
        lang: currentLang,
      };
      try {
        const r = await api("/api/answer", {
          method: "POST",
          body: JSON.stringify(reqBody),
        }, 6);
        lastAnswerReq = reqBody;
        setStats(r);

        if (!isUsableQuestion(r.next)) {
          throw new Error(
            "Il server non ha inviato la prossima domanda — riprova"
          );
        }
        pendingNext = r.next; // held until advanceFromCache paints it
        // Stash next as live so review → Successiva restores it (not the answered one)
        if (isUsableQuestion(r.next)) {
          liveQuestion = {
            id: r.next.id,
            question_it: r.next.question_it,
            question_tr: r.next.question_tr,
            image: r.next.image,
            lang: r.next.lang || currentLang,
            streak: r.next.streak,
            mistakes_30: r.next.mistakes_30,
            answered_30: r.next.answered_30,
            history_len: r.history_len != null ? r.history_len : historyLen,
          };
        }
        markUserAnswer(ans, r.correct);

        const fb = el("feedback");
        if (r.correct) {
          if (fb) {
            fb.className = "feedback ok";
            fb.innerHTML = `<h3>${escapeHtml(UI.correct)}</h3>`;
            fb.classList.remove("hidden");
          }
          setTimeout(() => {
            advanceFromCache().catch((e) =>
              showLoadError(e.message, () => advanceFromCache())
            );
          }, 500);
        } else {
          if (fb) {
            fb.className = "feedback bad";
            fb.innerHTML = `
              <h3>${escapeHtml(UI.wrong)}</h3>
              <p>${escapeHtml(r.explanation || "")}</p>
              <button type="button" class="primary" id="btn-continue">${escapeHtml(UI.continue)}</button>
            `;
            fb.classList.remove("hidden");
            const cont = el("btn-continue");
            if (cont) cont.onclick = () => advanceFromCache();
          }
          busy = false;
          updateNavUi();
        }
      } catch (e) {
        // Leave the current question on screen for another try
        const msg = e.message || "errore";
        setText("status", msg);
        showLoadError(msg, () => {
          const f = el("feedback");
          if (f) { f.classList.add("hidden"); f.innerHTML = ""; }
          setText("status", "");
          setAnswerEnabled(true);
          busy = false;
          updateNavUi();
        });
        setAnswerEnabled(true);
        busy = false;
        updateNavUi();
      }
    }

    function escapeHtml(s) {
      return String(s)
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;");
    }

    el("btn-v").onclick = () => answer("V");
    el("btn-f").onclick = () => answer("F");
    el("btn-prev").onclick = () => { goPrev().catch((e) => setText("status", e.message)); };
    el("btn-next").onclick = () => { goNext().catch((e) => setText("status", e.message)); };
    el("btn-change-lang").onclick = () => {
      currentId = null;
      pendingNext = null;
      lastAnswerReq = null;
      historyLen = 0;
      reviewIndex = null;
      liveQuestion = null;
      loadGen++; // drop in-flight loads
      busy = false;
      show("lang");
    };

    initLangScreen().catch((e) => {
      setText("status", e.message);
    });
  </script>
</body>
</html>
"""


@app.errorhandler(Exception)
def _unhandled(err):
    """Log unexpected errors; re-raise normal HTTPExceptions."""
    log.exception("Unhandled error: %s", err)
    try:
        from werkzeug.exceptions import HTTPException

        if isinstance(err, HTTPException):
            return err
    except Exception:
        pass
    return jsonify({"error": "server error", "detail": str(err)}), 500


def _rss_mb() -> float | None:
    """Resident set size in MiB from /proc, or None if unavailable."""
    try:
        with open("/proc/self/status", encoding="utf-8") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) / 1024.0
    except Exception:
        pass
    return None


_android_wake_lock = None  # retain reference so the lock is not GC'd


def _try_android_wake_lock() -> str:
    """
    Best-effort Android PARTIAL_WAKE_LOCK (CPU may stay on in the background).
    Returns a short status string; never raises.

    Set ANDROID_WAKELOCK=0 to skip (default tries once at startup).
    """
    global _android_wake_lock

    flag = os.environ.get("ANDROID_WAKELOCK", "1").strip().lower()
    if flag in ("0", "false", "off", "no"):
        return "wake lock disabled (ANDROID_WAKELOCK=0)"

    # pyjnius on some Android Python builds
    try:
        from jnius import autoclass  # type: ignore

        PythonActivity = None
        for name in (
            "com.chaquo.python.android.AndroidPlatform",
            "org.kivy.android.PythonActivity",
            "ru.iiec.pydroid3.MainActivity",
            "ru.iiec.pydroid3.Pydroid3Activity",
        ):
            try:
                PythonActivity = autoclass(name)
                break
            except Exception:
                continue

        context = None
        if PythonActivity is not None:
            try:
                context = PythonActivity.mActivity
            except Exception:
                try:
                    context = PythonActivity.mService
                except Exception:
                    context = None

        if context is None:
            try:
                ActivityThread = autoclass("android.app.ActivityThread")
                context = ActivityThread.currentApplication()
            except Exception:
                context = None

        if context is not None:
            Context = autoclass("android.content.Context")
            PowerManager = autoclass("android.os.PowerManager")
            pm = context.getSystemService(Context.POWER_SERVICE)
            tag = "PatenteQuiz:WakeLock"
            wl = pm.newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, tag)
            wl.setReferenceCounted(False)
            wl.acquire()
            _android_wake_lock = wl
            return "android PARTIAL_WAKE_LOCK acquired"
    except Exception as e:
        log.debug("jnius wake lock unavailable: %s", e)

    # SL4A / androidhelper (older embeds)
    try:
        import androidhelper  # type: ignore

        droid = androidhelper.Android()
        try:
            droid.wakeLockAcquirePartial()
            _android_wake_lock = droid
            return "androidhelper wakeLockAcquirePartial"
        except Exception:
            pass
    except Exception:
        pass

    return "no wake lock available"


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )

    it_path = DATA_DIR / "questions_it.json"
    if not it_path.exists():
        print(f"Missing {it_path}. Export the Anki deck first.")
        sys.exit(1)

    # Italian at startup; FR/DE load on demand (one bank in memory at a time)
    print("Loading Italian question bank (other languages load on demand)…", flush=True)
    try:
        n = len(load_questions("it"))
        print(f"  it: {n}", flush=True)
    except Exception as e:
        print(f"  it: FAIL {e}", flush=True)
        sys.exit(1)

    rss = _rss_mb()
    if rss is not None:
        print(f"  RSS after load: {rss:.1f} MB", flush=True)

    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", "5000"))

    wl_status = _try_android_wake_lock()
    print(f"Wake lock: {wl_status}", flush=True)

    print(f"Patente quiz running at http://{host}:{port}", flush=True)
    print("On Termux you can open it with: termux-open-url http://127.0.0.1:5000", flush=True)
    print(
        "Pydroid tip: keep the app in the foreground (split-screen with the browser). "
        "Android may still suspend a background Python process.",
        flush=True,
    )

    try:
        # threaded: images + API together; no reloader (avoids a second process)
        app.run(host=host, port=port, debug=False, threaded=True, use_reloader=False)
    except Exception:
        traceback.print_exc()
        raise
    finally:
        try:
            if _android_wake_lock is not None:
                if hasattr(_android_wake_lock, "release"):
                    if getattr(_android_wake_lock, "isHeld", lambda: True)():
                        _android_wake_lock.release()
                elif hasattr(_android_wake_lock, "wakeLockRelease"):
                    _android_wake_lock.wakeLockRelease()
        except Exception:
            pass


if __name__ == "__main__":
    main()
