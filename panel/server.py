# -*- coding: utf-8 -*-
"""
Локальный движок панели SignalKit.
UI: panel.html  |  API: http://127.0.0.1:8765
Запуск: START.bat
"""

from __future__ import annotations

import html
import json
import subprocess
import sys
import threading
import time
import traceback
import urllib.parse
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core import config as cfg  # noqa: E402

HOST = "127.0.0.1"
PORT = 8765
HTML_PATH = ROOT / "panel.html"
if not HTML_PATH.exists():
    HTML_PATH = ROOT / "Панель.html"

# Фоновые задачи: job_id -> state
JOBS: dict[str, dict] = {}
JOBS_LOCK = threading.Lock()
BUSY = False


def _job_log(job_id: str, msg: str) -> None:
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if not job:
            return
        job["log"].append(msg)
        if len(job["log"]) > 500:
            job["log"] = job["log"][-400:]
    print(f"[{job_id[:8]}] {msg}", flush=True)


def _run_job_worker(job_id: str, name: str, args: list[str]) -> None:
    global BUSY
    try:
        _job_log(job_id, f">>> Старт: {name}")
        _job_log(job_id, f"Команда: python {' '.join(args)}")
        proc = subprocess.Popen(
            [sys.executable, "-u", *args],
            cwd=str(ROOT),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
        assert proc.stdout is not None
        for line in proc.stdout:
            _job_log(job_id, line.rstrip())
        code = proc.wait()
        _job_log(job_id, f"<<< Готово, код={code}")
        with JOBS_LOCK:
            JOBS[job_id]["ok"] = code == 0
            JOBS[job_id]["done"] = True
            JOBS[job_id]["status"] = "done" if code == 0 else "error"
    except Exception:
        _job_log(job_id, traceback.format_exc())
        with JOBS_LOCK:
            JOBS[job_id]["ok"] = False
            JOBS[job_id]["done"] = True
            JOBS[job_id]["status"] = "error"
    finally:
        BUSY = False


def start_job(name: str, args: list[str]) -> dict:
    global BUSY
    with JOBS_LOCK:
        if BUSY:
            return {"ok": False, "error": "Уже выполняется другая задача. Подождите в журнале."}
        BUSY = True
        job_id = uuid.uuid4().hex
        JOBS[job_id] = {
            "id": job_id,
            "name": name,
            "status": "running",
            "done": False,
            "ok": None,
            "log": [f"Задача «{name}» создана…"],
            "started": time.time(),
        }
    t = threading.Thread(target=_run_job_worker, args=(job_id, name, args), daemon=True)
    t.start()
    return {"ok": True, "job_id": job_id, "message": f"Запущено: {name}"}


def job_status(job_id: str) -> dict:
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if not job:
            return {"ok": False, "error": "Задача не найдена", "done": True}
        return {
            "ok": True,
            "job_id": job_id,
            "name": job["name"],
            "status": job["status"],
            "done": job["done"],
            "success": job["ok"],
            "log": "\n".join(job["log"][-120:]),
            "lines": len(job["log"]),
        }


def _signals_summary() -> dict:
    import csv

    md = cfg.OUTPUT_DIR / "signals_latest.md"
    csv_path = cfg.OUTPUT_DIR / "signals_latest.csv"
    mt5_csv = cfg.OUTPUT_DIR / "mt5" / "signals.csv"
    if not csv_path.exists():
        return {
            "ok": False,
            "error": "Сигналы ещё не скачаны. Нажмите «1. Скачать сигналы».",
            "rows": [],
        }
    rows = []
    with csv_path.open(encoding="utf-8-sig", newline="") as f:
        for r in csv.DictReader(f):
            if str(r.get("parse_ok", "")).lower() not in ("true", "1", "yes"):
                continue
            rows.append(
                {
                    "date": r.get("date_utc", ""),
                    "symbol": r.get("symbol", ""),
                    "side": r.get("side", ""),
                    "order": r.get("order_type", ""),
                    "entry": r.get("entry", ""),
                    "sl": r.get("sl", ""),
                    "tp": r.get("tp", ""),
                    "link": r.get("link", ""),
                    "role": r.get("role", ""),
                    "action": r.get("action", ""),
                    "chain_id": r.get("chain_id", ""),
                }
            )
    fail_n = 0
    manage_n = 0
    chains = set()
    try:
        with csv_path.open(encoding="utf-8-sig", newline="") as f:
            all_rows = list(csv.DictReader(f))
        fail_n = sum(
            1
            for r in all_rows
            if str(r.get("parse_ok", "")).lower() not in ("true", "1", "yes")
        )
        manage_n = sum(1 for r in all_rows if (r.get("role") or "") == "manage")
        for r in all_rows:
            if str(r.get("parse_ok", "")).lower() in ("true", "1", "yes") and r.get("chain_id"):
                chains.add(str(r.get("chain_id")))
    except Exception:
        pass
    return {
        "ok": True,
        "count": len(rows),
        "fail": fail_n,
        "manage": manage_n,
        "chains": len(chains),
        "md_path": str(md),
        "csv_path": str(csv_path),
        "mt5_csv": str(mt5_csv),
        "rows": rows[:120],
        "message": (
            f"Разобрано: {len(rows)} (сопровождение: {manage_n}, цепочек: {len(chains)}, "
            f"не разобрано: {fail_n}). "
            "Цепочка = исходный сигнал + корректирующие посты по тому же символу."
        ),
    }


def _backtest_summary() -> dict:
    import csv

    md = cfg.OUTPUT_DIR / "backtest" / "backtest_latest.md"
    csv_path = cfg.OUTPUT_DIR / "backtest" / "backtest_latest.csv"
    if not md.exists() or not csv_path.exists():
        return {
            "ok": False,
            "error": "Анализа ещё нет. Сначала шаг 1, потом «2. Анализ сделок» (MT5 должен быть открыт).",
            "rows": [],
        }
    md_text = md.read_text(encoding="utf-8", errors="replace")
    rows = []
    with csv_path.open(encoding="utf-8-sig", newline="") as f:
        for r in csv.DictReader(f):
            rows.append(
                {
                    "date": r.get("time_utc", ""),
                    "symbol": r.get("csv_symbol", ""),
                    "side": r.get("side", ""),
                    "outcome": r.get("outcome", ""),
                    "pnl_R": r.get("pnl_R", ""),
                    "entry": r.get("entry_signal", ""),
                    "sl": r.get("sl", ""),
                    "tp": r.get("tp", ""),
                    "chain_id": r.get("chain_id", ""),
                    "note": r.get("note", ""),
                }
            )
    tp = sum(1 for r in rows if r["outcome"] == "TP")
    sl = sum(1 for r in rows if r["outcome"] == "SL")
    manual = sum(1 for r in rows if r["outcome"] == "MANUAL")
    cancelled = sum(1 for r in rows if r["outcome"] == "CANCELLED")
    wr = round(tp / (tp + sl) * 100, 1) if tp + sl else 0.0
    traded = [r for r in rows if r["outcome"] in ("TP", "SL", "MANUAL")]
    try:
        sum_r = round(sum(float(r["pnl_R"] or 0) for r in traded), 2)
    except Exception:
        sum_r = 0.0
    return {
        "ok": True,
        "count": len(rows),
        "tp": tp,
        "sl": sl,
        "manual": manual,
        "cancelled": cancelled,
        "winrate": wr,
        "sum_R": sum_r,
        "md_path": str(md),
        "csv_path": str(csv_path),
        "md_text": md_text,
        "rows": rows[:120],
        "message": (
            f"Анализ M1 (ранние без минутной истории — H1): {len(rows)} сделок → "
            f"TP {tp} / SL {sl} / закрыто по посту {manual} / лимит снят {cancelled}, "
            f"винрейт {wr}%, сумма {sum_r}R."
        ),
    }


def _html_page(text: str) -> bytes:
    return (
        f"<html><meta charset=utf-8><body style='font-family:sans-serif;padding:24px'>{text}</body></html>"
    ).encode("utf-8")


_VIEW_CSS = """
:root{
  --ink:#1a1f1c; --muted:#5c6560; --line:#d8d3c8;
  --go:#1f6f4a; --bad:#8b2e2e; --warn:#8a5a00; --accent:#c4a35a;
  --tp-bg:#e7f3ec; --tp-bd:#9fd0b4; --sl-bg:#fdeeee; --sl-bd:#e8b4b4;
  --card:#fff;
}
*{box-sizing:border-box}
body{margin:0;background:linear-gradient(165deg,#ebe6db 0%,#f7f5f0 45%,#e7eee8 100%);
  color:var(--ink);font:15px/1.45 "Segoe UI",system-ui,sans-serif;min-height:100vh}
.wrap{max-width:1100px;margin:0 auto;padding:28px 18px 60px}
h1{font-size:1.55rem;margin:0 0 6px;letter-spacing:-.02em}
.sub{color:var(--muted);margin:0 0 18px}
.card{background:var(--card);border:1px solid var(--line);border-radius:14px;
  padding:18px;box-shadow:0 8px 24px rgba(26,31,28,.04);margin-bottom:16px}
.metrics{display:flex;flex-wrap:wrap;gap:8px;margin:0 0 14px}
.metric{padding:8px 12px;border-radius:10px;background:#fcfbf8;border:1px solid var(--line)}
.metric b{font-size:1.05rem}
.metric.good{background:var(--tp-bg);border-color:var(--tp-bd);color:var(--go)}
.metric.bad{background:var(--sl-bg);border-color:var(--sl-bd);color:var(--bad)}
.metric.warn{background:#fff7e6;border-color:#e6c98a;color:var(--warn)}
table{width:100%;border-collapse:collapse;font-size:.86rem}
th,td{border-bottom:1px solid var(--line);padding:9px 10px;text-align:left;white-space:nowrap}
th{background:#f3efe6;position:sticky;top:0;z-index:1;font-weight:600}
tr.tp{background:var(--tp-bg)}
tr.sl{background:var(--sl-bg)}
tr.other{background:#faf8f3}
tr:hover{filter:brightness(.98)}
.badge{display:inline-block;padding:2px 8px;border-radius:999px;font-size:.78rem;font-weight:700}
.badge.tp{background:var(--go);color:#fff}
.badge.sl{background:var(--bad);color:#fff}
.badge.ok{background:var(--go);color:#fff}
.badge.fail{background:var(--bad);color:#fff}
.badge.neutral{background:#e8e4da;color:var(--ink)}
.pnl-pos{color:var(--go);font-weight:700}
.pnl-neg{color:var(--bad);font-weight:700}
a{color:var(--go)}
.hint{color:var(--muted);font-size:.88rem;margin-top:10px}
.scroll{overflow:auto;max-height:70vh;border:1px solid var(--line);border-radius:12px}
.nav{margin-bottom:14px}
.nav a{margin-right:12px;text-decoration:none;font-weight:600}
.raw{max-width:280px;white-space:normal;font-size:.78rem;color:var(--muted)}
"""


def _view_shell(title: str, body: str) -> bytes:
    html_doc = f"""<!DOCTYPE html>
<html lang="ru"><head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>{html.escape(title)}</title>
<style>{_VIEW_CSS}</style>
</head><body><div class="wrap">{body}</div></body></html>"""
    return html_doc.encode("utf-8")


def _fmt_num(v: str | float | int, digits: int = 5) -> str:
    try:
        f = float(v)
        if f == 0:
            return "—"
        s = f"{f:.{digits}f}".rstrip("0").rstrip(".")
        return s or "0"
    except Exception:
        return str(v or "—")


def _render_signals_page() -> bytes:
    import csv

    csv_path = cfg.OUTPUT_DIR / "signals_latest.csv"
    if not csv_path.exists():
        return _view_shell(
            "Сигналы",
            "<h1>Сигналы</h1><p class='sub'>Пока нет данных. В панели нажмите «1. Скачать сигналы».</p>",
        )

    ok_rows: list[dict] = []
    fail_rows: list[dict] = []
    with csv_path.open(encoding="utf-8-sig", newline="") as f:
        for r in csv.DictReader(f):
            if str(r.get("parse_ok", "")).lower() in ("true", "1", "yes"):
                ok_rows.append(r)
            else:
                fail_rows.append(r)

    metrics = (
        f"<div class='metrics'>"
        f"<span class='metric good'>Разобрано: <b>{len(ok_rows)}</b></span>"
        f"<span class='metric bad'>Не разобрано: <b>{len(fail_rows)}</b></span>"
        f"<span class='metric'>Всего кандидатов: <b>{len(ok_rows) + len(fail_rows)}</b></span>"
        f"</div>"
    )

    def row_ok(r: dict) -> str:
        link = html.escape(r.get("link") or "#")
        role = (r.get("role") or "").strip()
        action = (r.get("action") or "").strip()
        action_ru = {
            "open": "вход",
            "breakeven": "безубыток",
            "modify_sl": "смена стопа",
            "modify_levels": "смена уровней",
            "replace_market": "замена на рынок",
            "cancel_pending": "снятие лимита",
            "reverse": "разворот",
            "close": "закрытие",
            "add": "добор",
        }.get(action, action)
        role_ru = {"signal": "сигнал", "manage": "сопровождение"}.get(role, role)
        cls = "tp" if action == "breakeven" else ("sl" if role == "manage" else "other")
        return (
            f"<tr class='{cls}'>"
            f"<td>{html.escape(r.get('date_utc',''))}</td>"
            f"<td>{html.escape(role_ru)}</td>"
            f"<td>{html.escape(str(r.get('chain_id') or ''))}</td>"
            f"<td><b>{html.escape(r.get('symbol',''))}</b></td>"
            f"<td>{html.escape(r.get('side',''))}</td>"
            f"<td><b>{html.escape(action_ru)}</b></td>"
            f"<td>{html.escape(r.get('order_type',''))}</td>"
            f"<td>{html.escape(_fmt_num(r.get('entry','')))}</td>"
            f"<td>{html.escape(_fmt_num(r.get('sl','')))}</td>"
            f"<td>{html.escape(_fmt_num(r.get('tp','')))}</td>"
            f"<td><a href='{link}' target='_blank' rel='noopener'>пост</a></td>"
            "</tr>"
        )

    ok_table = (
        "<div class='scroll'><table><thead><tr>"
        "<th>Дата UTC</th><th>Роль</th><th>Цепочка</th><th>Символ</th><th>Сторона</th>"
        "<th>Действие</th><th>Ордер</th><th>Entry</th><th>SL</th><th>TP</th><th>Пост</th>"
        "</tr></thead><tbody>"
        + "".join(row_ok(r) for r in ok_rows)
        + "</tbody></table></div>"
    )

    fail_block = ""
    if fail_rows:
        fail_trs = []
        for r in fail_rows[:120]:
            raw = (r.get("raw_text") or "").replace("\n", " ")[:160]
            link = html.escape(r.get("link") or "#")
            fail_trs.append(
                "<tr class='sl'>"
                f"<td>{html.escape(r.get('date_utc',''))}</td>"
                f"<td>{html.escape(r.get('symbol') or '—')}</td>"
                f"<td><span class='badge fail'>fail</span></td>"
                f"<td>{html.escape(r.get('parse_note') or '')}</td>"
                f"<td class='raw'>{html.escape(raw)}</td>"
                f"<td><a href='{link}' target='_blank' rel='noopener'>пост</a></td>"
                "</tr>"
            )
        fail_block = (
            "<div class='card'><h2 style='margin:0 0 8px;font-size:1.05rem'>Не разобрано</h2>"
            "<p class='hint' style='margin-top:0'>Посты-кандидаты без полного набора "
            "symbol / сторона / entry / SL (часто управление позицией, не новый сигнал).</p>"
            "<div class='scroll'><table><thead><tr>"
            "<th>Дата</th><th>Символ</th><th></th><th>Причина</th><th>Текст</th><th>Пост</th>"
            "</tr></thead><tbody>"
            + "".join(fail_trs)
            + "</tbody></table></div></div>"
        )

    body = (
        "<div class='nav'><a href='/'>← Панель</a><a href='/report'>Анализ сделок →</a></div>"
        "<h1>Сигналы канала</h1>"
        "<p class='sub'>Разобранные торговые идеи из Telegram. Это ещё не прибыль — только список сделок.</p>"
        f"<div class='card'>{metrics}{ok_table}"
        "<p class='hint'>Источник: output/signals_latest.csv</p></div>"
        f"{fail_block}"
    )
    return _view_shell("Сигналы — SignalKit", body)


def _render_report_page() -> bytes:
    import csv

    csv_path = cfg.OUTPUT_DIR / "backtest" / "backtest_latest.csv"
    if not csv_path.exists():
        return _view_shell(
            "Анализ",
            "<h1>Анализ сделок</h1><p class='sub'>Пока нет отчёта. В панели: шаг 1 → шаг 2 (MT5 открыт).</p>",
        )

    rows: list[dict] = []
    with csv_path.open(encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))

    tp = sum(1 for r in rows if r.get("outcome") == "TP")
    sl = sum(1 for r in rows if r.get("outcome") == "SL")
    other = len(rows) - tp - sl
    traded = [r for r in rows if r.get("outcome") in ("TP", "SL")]
    try:
        sum_r = round(sum(float(r.get("pnl_R") or 0) for r in traded), 2)
    except Exception:
        sum_r = 0.0
    wr = round(tp / (tp + sl) * 100, 1) if tp + sl else 0.0
    sum_cls = "good" if sum_r >= 0 else "bad"

    metrics = (
        f"<div class='metrics'>"
        f"<span class='metric'>Сделок: <b>{len(rows)}</b></span>"
        f"<span class='metric good'>TP: <b>{tp}</b></span>"
        f"<span class='metric bad'>SL: <b>{sl}</b></span>"
        f"<span class='metric'>Winrate: <b>{wr}%</b></span>"
        f"<span class='metric {sum_cls}'>Сумма R: <b>{sum_r:+.2f}</b></span>"
        + (f"<span class='metric warn'>Прочее: <b>{other}</b></span>" if other else "")
        + "</div>"
    )

    trs = []
    for r in rows:
        outcome = (r.get("outcome") or "").upper()
        cls = "tp" if outcome == "TP" else "sl" if outcome == "SL" else "other"
        badge = (
            f"<span class='badge tp'>TP</span>"
            if outcome == "TP"
            else f"<span class='badge sl'>SL</span>"
            if outcome == "SL"
            else f"<span class='badge neutral'>{html.escape(outcome or '—')}</span>"
        )
        try:
            pnl = float(r.get("pnl_R") or 0)
            pnl_html = (
                f"<span class='pnl-pos'>{pnl:+.2f}R</span>"
                if pnl > 0
                else f"<span class='pnl-neg'>{pnl:+.2f}R</span>"
                if pnl < 0
                else f"<span>{pnl:+.2f}R</span>"
            )
        except Exception:
            pnl_html = html.escape(str(r.get("pnl_R") or "—"))

        trs.append(
            f"<tr class='{cls}'>"
            f"<td>{html.escape(r.get('time_utc',''))}</td>"
            f"<td>{html.escape(str(r.get('chain_id') or ''))}</td>"
            f"<td><b>{html.escape(r.get('csv_symbol',''))}</b></td>"
            f"<td>{html.escape(r.get('side',''))}</td>"
            f"<td>{html.escape(r.get('order_type',''))}</td>"
            f"<td>{html.escape(_fmt_num(r.get('entry_signal','')))}</td>"
            f"<td>{html.escape(_fmt_num(r.get('sl','')))}</td>"
            f"<td>{html.escape(_fmt_num(r.get('tp','')))}</td>"
            f"<td>{badge}</td>"
            f"<td>{pnl_html}</td>"
            f"<td>{html.escape(r.get('bars_held') or '—')}</td>"
            f"<td>{html.escape(r.get('note') or '—')}</td>"
            "</tr>"
        )

    table = (
        "<div class='scroll'><table><thead><tr>"
        "<th>Дата</th><th>Цепочка</th><th>Символ</th><th>Сторона</th><th>Ордер</th>"
        "<th>Entry</th><th>SL</th><th>TP</th><th>Итог</th><th>PnL</th><th>Баров</th><th>Заметка</th>"
        "</tr></thead><tbody>"
        + "".join(trs)
        + "</tbody></table></div>"
    )

    body = (
        "<div class='nav'><a href='/'>← Панель</a><a href='/signals'>← Сигналы</a></div>"
        "<h1>Анализ сделок</h1>"
        "<p class='sub'>Бэктест по истории MT5 (M1). Зелёный = TP (прибыль), красный = SL (убыток).</p>"
        f"<div class='card'>{metrics}{table}"
        "<p class='hint'>Источник: output/backtest/backtest_latest.csv · "
        "После правки парсера перезапустите шаг 2, чтобы обновить анализ.</p></div>"
    )
    return _view_shell("Анализ — SignalKit", body)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def _send(self, code: int, body: bytes, ctype: str = "text/html; charset=utf-8") -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj: dict, code: int = 200) -> None:
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self._send(code, data, "application/json; charset=utf-8")

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self) -> None:
        path = urllib.parse.urlparse(self.path).path
        if path in ("/", "/index.html", "/panel.html", "/Панель.html"):
            if not HTML_PATH.exists():
                self._send(500, _html_page("panel.html not found"))
                return
            # no-cache HTML so buttons always fresh
            body = HTML_PATH.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store, max-age=0")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if path == "/api/ping":
            self._json({"ok": True, "busy": BUSY})
            return
        if path.startswith("/api/job/"):
            job_id = path.rsplit("/", 1)[-1]
            self._json(job_status(job_id))
            return
        if path == "/api/results/signals":
            self._json(_signals_summary())
            return
        if path == "/api/results/backtest":
            self._json(_backtest_summary())
            return
        if path == "/api/tg/status":
            from core import tg_auth

            self._json(tg_auth.status())
            return
        if path == "/api/settings":
            cp = cfg.load_settings()
            self._json(
                {
                    "channel": cfg.get(cp, "telegram", "channel"),
                    "fetch_mode": cfg.get(cp, "telegram", "fetch_mode", "telethon"),
                    "api_id": cfg.get(cp, "telegram", "api_id"),
                    "api_hash": cfg.get(cp, "telegram", "api_hash"),
                    "phone": cfg.get(cp, "telegram", "phone"),
                    "days_back": cfg.get(cp, "period", "days_back", "365"),
                    "format": cfg.get(cp, "parse", "format", "labels"),
                    "must_contain": cfg.get(cp, "parse", "must_contain"),
                    "skip_if_contains": cfg.get(cp, "parse", "skip_if_contains"),
                    "label_side": cfg.get(cp, "parse", "label_side"),
                    "label_entry": cfg.get(cp, "parse", "label_entry"),
                    "label_sl": cfg.get(cp, "parse", "label_sl"),
                    "label_tp": cfg.get(cp, "parse", "label_tp"),
                    "buy_words": cfg.get(cp, "parse", "buy_words"),
                    "sell_words": cfg.get(cp, "parse", "sell_words"),
                    "tp_open_words": cfg.get(cp, "parse", "tp_open_words"),
                    "open_tp_rr": cfg.get(cp, "parse", "open_tp_rr", "2.0"),
                    "manage_enabled": cfg.get(cp, "manage", "enabled", "no"),
                    "manage_link_max_hours": cfg.get(cp, "manage", "link_max_hours", "96"),
                    "manage_words_cancel_pending": cfg.get(cp, "manage", "words_cancel_pending"),
                    "manage_words_reverse": cfg.get(cp, "manage", "words_reverse"),
                    "manage_words_modify_sl": cfg.get(cp, "manage", "words_modify_sl"),
                    "manage_words_to_market": cfg.get(cp, "manage", "words_to_market"),
                    "manage_words_close": cfg.get(cp, "manage", "words_close"),
                    "manage_words_breakeven": cfg.get(cp, "manage", "words_breakeven"),
                }
            )
            return
        if path == "/open-settings":
            cfg.ensure_settings()
            try:
                subprocess.Popen(["notepad.exe", str(cfg.SETTINGS_PATH)])
            except Exception:
                pass
            self._send(200, _html_page(f"Opened:<br><code>{html.escape(str(cfg.SETTINGS_PATH))}</code>"))
            return
        if path == "/report":
            self._send(200, _render_report_page())
            return
        if path == "/signals":
            self._send(200, _render_signals_page())
            return
        self._send(404, _html_page("Not found"))

    def do_POST(self) -> None:
        path = urllib.parse.urlparse(self.path).path
        length = int(self.headers.get("Content-Length", 0) or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            data = json.loads(raw.decode("utf-8") or "{}")
        except json.JSONDecodeError:
            data = {}

        if path == "/api/settings":
            try:
                cp = cfg.load_settings()
                for sec in ("telegram", "period", "parse", "trade", "manage"):
                    if not cp.has_section(sec):
                        cp.add_section(sec)
                mapping = {
                    ("telegram", "channel"): "channel",
                    ("telegram", "fetch_mode"): "fetch_mode",
                    ("telegram", "api_id"): "api_id",
                    ("telegram", "api_hash"): "api_hash",
                    ("telegram", "phone"): "phone",
                    ("period", "days_back"): "days_back",
                    ("parse", "format"): "format",
                    ("parse", "must_contain"): "must_contain",
                    ("parse", "skip_if_contains"): "skip_if_contains",
                    ("parse", "label_side"): "label_side",
                    ("parse", "label_entry"): "label_entry",
                    ("parse", "label_sl"): "label_sl",
                    ("parse", "label_tp"): "label_tp",
                    ("parse", "buy_words"): "buy_words",
                    ("parse", "sell_words"): "sell_words",
                    ("parse", "tp_open_words"): "tp_open_words",
                    ("parse", "open_tp_rr"): "open_tp_rr",
                    ("manage", "enabled"): "manage_enabled",
                    ("manage", "link_max_hours"): "manage_link_max_hours",
                    ("manage", "words_cancel_pending"): "manage_words_cancel_pending",
                    ("manage", "words_reverse"): "manage_words_reverse",
                    ("manage", "words_modify_sl"): "manage_words_modify_sl",
                    ("manage", "words_to_market"): "manage_words_to_market",
                    ("manage", "words_close"): "manage_words_close",
                    ("manage", "words_breakeven"): "manage_words_breakeven",
                }
                for (sec, key), form_key in mapping.items():
                    if form_key in data:
                        cp.set(sec, key, str(data[form_key]).strip())
                cfg.save_settings(cp)
                cfg.export_live_rules(cp)
                self._json({"ok": True})
            except Exception as e:
                self._json({"ok": False, "error": str(e)}, 400)
            return

        if path.startswith("/api/preset/"):
            name = path.rsplit("/", 1)[-1]
            try:
                cp = cfg.load_settings()
                cfg.apply_preset(cp, name)
                if not cp.has_section("parse"):
                    cp.add_section("parse")
                cp.set("parse", "preset", name)
                cfg.save_settings(cp)
                self._json({"ok": True})
            except Exception as e:
                self._json({"ok": False, "error": str(e)}, 400)
            return

        if path == "/api/tg/send_code":
            from core import tg_auth

            phone = (data.get("phone") or "").strip() or None
            force_sms = bool(data.get("force_sms"))
            self._json(tg_auth.send_code(phone, force_sms=force_sms))
            return
        if path == "/api/tg/import_session":
            from core import tg_auth

            self._json(tg_auth.import_existing_session())
            return
        if path == "/api/tg/submit_code":
            from core import tg_auth

            self._json(tg_auth.submit_code(str(data.get("code") or "")))
            return
        if path == "/api/tg/submit_password":
            from core import tg_auth

            self._json(tg_auth.submit_password(str(data.get("password") or "")))
            return

        if path.startswith("/api/run/"):
            name = path.rsplit("/", 1)[-1]
            if name == "parse":
                self._json(start_job("parse", ["jobs/run_parse.py"]))
                return
            if name == "backtest":
                self._json(start_job("backtest", ["jobs/run_backtest.py"]))
                return
            if name == "live_prep":
                try:
                    cp = cfg.load_settings()
                    path_rules = cfg.export_live_rules(cp)
                    ea_src = ROOT / "MT5" / "TG_Signal_Live_EA.mq5"
                    experts = Path.home() / "AppData/Roaming/MetaQuotes/Terminal"
                    lines = [f"Rules: {path_rules}"]
                    if experts.exists() and ea_src.exists():
                        for term in experts.iterdir():
                            exp = term / "MQL5" / "Experts"
                            if exp.is_dir():
                                dest = exp / ea_src.name
                                dest.write_bytes(ea_src.read_bytes())
                                lines.append(f"EA copied: {dest}")
                    lines.append("MT5: Navigator -> Experts -> Refresh -> TG_Signal_Live_EA")
                    lines.append("Allow WebRequest: https://t.me | InpDryRun=true first")
                    self._json({"ok": True, "log": "\n".join(lines), "done": True})
                except Exception as e:
                    self._json({"ok": False, "error": str(e), "log": traceback.format_exc()}, 500)
                return
            self._json({"ok": False, "error": "unknown job"}, 404)
            return

        self._send(404, _html_page("Not found"))


def main() -> None:
    cfg.ensure_settings()
    cp = cfg.load_settings()
    if not cfg.get(cp, "parse", "format"):
        try:
            preset = cfg.get(cp, "parse", "preset", "custom") or "custom"
            cfg.apply_preset(cp, preset)
            cfg.save_settings(cp)
        except Exception:
            pass

    if not HTML_PATH.exists():
        print("ERROR: panel.html not found:", HTML_PATH)
        input("Press Enter...")
        return

    try:
        server = ThreadingHTTPServer((HOST, PORT), Handler)
    except OSError as e:
        print("ERROR: cannot bind port", PORT, e)
        print("Maybe panel already running. Open http://127.0.0.1:8765/")
        input("Press Enter...")
        return

    url = f"http://{HOST}:{PORT}/"
    print("============================================")
    print("  SignalKit panel is running")
    print("  Open:", url)
    print("  Keep this window open while using the panel")
    print("============================================")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
