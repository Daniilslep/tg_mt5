# -*- coding: utf-8 -*-
"""Универсальный бэктест сигналов по истории MT5 с сопровождением сделок."""

from __future__ import annotations

import csv
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import config as cfg


@dataclass
class TradeResult:
    msg_id: str
    time_utc: str
    csv_symbol: str
    broker_symbol: str
    side: str
    order_type: str
    entry_signal: float
    sl: float
    tp: float
    open_price: float
    close_price: float
    outcome: str
    pnl_R: float
    bars_held: int
    note: str = ""
    chain_id: str = ""
    root_id: str = ""


TF_MAP = {
    "M1": "TIMEFRAME_M1",
    "M5": "TIMEFRAME_M5",
    "M15": "TIMEFRAME_M15",
    "M30": "TIMEFRAME_M30",
    "H1": "TIMEFRAME_H1",
    "H4": "TIMEFRAME_H4",
    "D1": "TIMEFRAME_D1",
}


def backtest_timeframe(cp=None) -> str:
    if cp is None:
        cp = cfg.load_settings()
    tf = (
        cfg.get(cp, "backtest", "timeframe", "")
        or cfg.get(cp, "trade", "backtest_tf", "M1")
        or "M1"
    )
    return tf.strip().upper() or "M1"


def mt5_timeframe(mt5, tf_name: str):
    key = TF_MAP.get(tf_name.upper(), "TIMEFRAME_M1")
    return getattr(mt5, key, mt5.TIMEFRAME_M1)


def base_key(sym: str) -> str:
    s = sym.upper().strip()
    if "." in s:
        s = s.split(".", 1)[0]
    for tail in ("PRO", "MINI", "MICRO", "RAW", "ECN", "STP"):
        if s.endswith(tail) and len(s) > len(tail):
            s = s[: -len(tail)]
            break
    if len(s) > 6 and s[-1] in "MIC":
        s = s[:-1]
    return s


def resolve_symbol(mt5, csv_sym: str) -> str | None:
    for c in (csv_sym, csv_sym.upper()):
        if mt5.symbol_info(c) is not None:
            mt5.symbol_select(c, True)
            return c
    want = base_key(csv_sym)
    for info in mt5.symbols_get() or []:
        if base_key(info.name) == want:
            mt5.symbol_select(info.name, True)
            return info.name
    return None


def direction_ok(side: str, entry: float, sl: float, tp: float) -> bool:
    if entry <= 0 or sl <= 0 or tp <= 0:
        return False
    su = (side or "").upper()
    if su.startswith("B"):
        return sl < entry < tp
    return tp < entry < sl


def pnl_R(side, open_px, close_px, sl):
    risk = abs(open_px - sl)
    if risk <= 0 or open_px <= 0:
        return 0.0
    if side == "BUY":
        return (close_px - open_px) / risk
    return (open_px - close_px) / risk


def _bars_needed_for_span(start_utc: datetime, tf_name: str) -> int:
    """Грубая оценка числа баров от start до сейчас (+ запас)."""
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    if start_utc.tzinfo is not None:
        start_utc = start_utc.astimezone(timezone.utc).replace(tzinfo=None)
    days = max(1.0, (now - start_utc).total_seconds() / 86400.0 + 7)
    tf = tf_name.upper()
    minutes = 1
    if tf.startswith("M") and tf[1:].isdigit():
        minutes = int(tf[1:])
    elif tf.startswith("H") and tf[1:].isdigit():
        minutes = 60 * int(tf[1:])
    elif tf.startswith("D"):
        minutes = 1440
    # forex ~5/7 суток
    return int(days * (1440 / minutes) * (5 / 7) * 1.15) + 5000


def ensure_mt5_maxbars(mt5, min_bars: int = 1_000_000) -> bool:
    """
    MT5 API отдаёт не больше MaxBars баров (~100000 ≈ 3 мес M1).
    Поднимаем MaxBars в common.ini. Возвращает True, если лимит уже достаточен.
    Если нет — правим ini и печатаем предупреждение (историю всё равно можно
    добрать из локальных .hcc).
    """
    info = mt5.terminal_info()
    if info is None:
        raise RuntimeError("MT5: нет terminal_info()")
    current = int(getattr(info, "maxbars", 0) or 0)
    data_path = Path(str(info.data_path))
    ini = data_path / "config" / "common.ini"
    target = max(min_bars, 1_000_000)

    if current >= min_bars:
        print(f"MT5 MaxBars={current} (достаточно для M1)", flush=True)
        return True

    patched = False
    if ini.exists():
        raw = ini.read_bytes()
        enc = "utf-16" if raw[:2] in (b"\xff\xfe", b"\xfe\xff") else "utf-8"
        text = ini.read_text(encoding=enc)
        if "MaxBars=" in text:
            import re

            new_text, n = re.subn(
                r"(?m)^MaxBars=\d+",
                f"MaxBars={target}",
                text,
                count=1,
            )
            if n and new_text != text:
                ini.write_text(new_text, encoding=enc)
                patched = True
        if not patched and "[Charts]" in text:
            text = text.replace("[Charts]", f"[Charts]\nMaxBars={target}", 1)
            ini.write_text(text, encoding=enc)
            patched = True

    print(
        f"! MT5 MaxBars={current} (~{max(1, current // 28800)} мес. M1). "
        f"Для полного API-доступа нужно ≥{min_bars}.",
        flush=True,
    )
    print(
        "  Рекомендуется: Сервис → Настройки → Графики → макс. баров = Unlimited, "
        "затем полный перезапуск MT5.",
        flush=True,
    )
    if patched:
        print(f"  Уже прописано MaxBars={target} в {ini}", flush=True)
    print("  Пока догружаю M1 из локальных файлов истории (.hcc)…", flush=True)
    return False


def _history_dir(mt5, broker: str) -> Path | None:
    info = mt5.terminal_info()
    acc = mt5.account_info()
    if info is None or acc is None:
        return None
    base = Path(str(info.data_path)) / "bases" / str(acc.server) / "history" / broker
    if base.is_dir():
        return base
    # иногда имя сервера в пути отличается регистром/дефисом
    bases = Path(str(info.data_path)) / "bases"
    if not bases.is_dir():
        return None
    for srv in bases.iterdir():
        cand = srv / "history" / broker
        if cand.is_dir():
            return cand
    return None


def _hcc_cache_dir(broker: str) -> Path:
    d = cfg.OUTPUT_DIR / "history_hcc" / broker
    d.mkdir(parents=True, exist_ok=True)
    return d


def _try_copy_hcc(src: Path, dst: Path) -> bool:
    import shutil

    try:
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        return dst.exists() and dst.stat().st_size > 1000
    except OSError:
        return False


def _restart_mt5_to_unlock_hcc(mt5, copy_jobs: list[tuple[Path, Path]]) -> bool:
    """Кратко закрывает MT5, копирует заблокированные .hcc, снова открывает."""
    import shutil
    import subprocess
    import time

    info = mt5.terminal_info()
    if info is None:
        return False
    term = Path(str(info.path)) / "terminal64.exe"
    if not term.exists():
        term = Path(str(info.path)) / "terminal.exe"
    if not term.exists():
        print(f"  ! не найден terminal.exe в {info.path}", flush=True)
        return False

    print(
        "  MT5 блокирует файлы истории текущего года (.hcc). "
        "На 10–20 сек перезапускаю терминал, чтобы скопировать M1…",
        flush=True,
    )
    mt5.shutdown()
    subprocess.run(
        ["taskkill", "/IM", "terminal64.exe", "/F"],
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ["taskkill", "/IM", "terminal.exe", "/F"],
        capture_output=True,
        text=True,
    )
    time.sleep(2.5)

    ok_n = 0
    for src, dst in copy_jobs:
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            ok_n += 1
            print(f"      скопирован {src.parent.name}/{src.name} → cache", flush=True)
        except OSError as exc:
            print(f"      ! не скопирован {src}: {exc}", flush=True)

    subprocess.Popen([str(term)], cwd=str(term.parent))
    for i in range(90):
        time.sleep(1)
        if mt5.initialize():
            acc = mt5.account_info()
            if acc is not None:
                print(f"  MT5 снова онлайн ({acc.server}), скопировано файлов: {ok_n}", flush=True)
                return ok_n > 0
        if i in (10, 25, 45, 70):
            print(f"  …жду вход в MT5 ({i}s)", flush=True)
    print("  ! MT5 не поднялся после перезапуска — продолжаю с тем, что есть", flush=True)
    mt5.initialize()
    return ok_n > 0


def _prefetch_hcc_for_symbols(mt5, brokers: list[str], start_utc: datetime, end_utc: datetime) -> None:
    """Готовит читаемые копии .hcc (при необходимости один раз перезапускает MT5)."""
    if start_utc.tzinfo is not None:
        start_utc = start_utc.astimezone(timezone.utc).replace(tzinfo=None)
    if end_utc.tzinfo is not None:
        end_utc = end_utc.astimezone(timezone.utc).replace(tzinfo=None)
    years = list(range(start_utc.year, end_utc.year + 1))
    locked_jobs: list[tuple[Path, Path]] = []

    for broker in brokers:
        hist = _history_dir(mt5, broker)
        if hist is None:
            continue
        cache = _hcc_cache_dir(broker)
        for y in years:
            src = hist / f"{y}.hcc"
            if not src.exists() or src.stat().st_size < 1000:
                continue
            dst = cache / f"{y}.hcc"
            # актуальный год — обновляем кэш, если исходник доступен
            if _try_copy_hcc(src, dst):
                continue
            # файл есть, но заблокирован MT5 — нужен snapshot (в т.ч. прошлые годы)
            locked_jobs.append((src, dst))

    if locked_jobs:
        # уникальные src
        uniq: dict[str, tuple[Path, Path]] = {}
        for src, dst in locked_jobs:
            uniq[str(src)] = (src, dst)
        _restart_mt5_to_unlock_hcc(mt5, list(uniq.values()))


def _load_rates_from_hcc(mt5, broker: str, start_utc: datetime, end_utc: datetime | None = None):
    """Чтение M1 напрямую из .hcc / кэша SignalKit (обходит лимит MaxBars и блокировку MT5)."""
    try:
        from hcc_reader import read_hcc
    except ImportError:
        print(
            "  ! пакет hcc-reader не установлен — pip install git+https://github.com/hungpixi/hcc-reader.git",
            flush=True,
        )
        return None

    import pandas as pd

    hist = _history_dir(mt5, broker)
    cache = _hcc_cache_dir(broker)

    if start_utc.tzinfo is not None:
        start_utc = start_utc.astimezone(timezone.utc).replace(tzinfo=None)
    if end_utc is None:
        end_utc = datetime.now(timezone.utc).replace(tzinfo=None)
    elif end_utc.tzinfo is not None:
        end_utc = end_utc.astimezone(timezone.utc).replace(tzinfo=None)

    years = range(start_utc.year, end_utc.year + 1)
    parts = []
    for y in years:
        candidates = []
        if hist is not None:
            candidates.append(hist / f"{y}.hcc")
        candidates.append(cache / f"{y}.hcc")
        dfy = None
        used = None
        for fp in candidates:
            if not fp.exists() or fp.stat().st_size < 1000:
                continue
            try:
                dfy = read_hcc(str(fp))
                used = fp
                break
            except Exception:
                continue
        if dfy is None or len(dfy) == 0:
            print(f"  · {broker}/{y}.hcc недоступен", flush=True)
            continue
        parts.append(dfy)
        tag = "cache" if used and used.parent == cache else "live"
        print(f"      hcc {broker}/{y}.hcc → {len(dfy)} bars ({tag})", flush=True)

    if not parts:
        return None

    df = pd.concat(parts, ignore_index=True)
    if "datetime" in df.columns:
        df = df.rename(columns={"datetime": "time"})
    df["time"] = pd.to_datetime(df["time"], utc=True).dt.tz_localize(None)
    keep = [
        c
        for c in ("time", "open", "high", "low", "close", "tick_volume", "spread", "real_volume")
        if c in df.columns
    ]
    df = df[keep].drop_duplicates("time").sort_values("time").reset_index(drop=True)
    df = df[(df["time"] >= start_utc - timedelta(days=1)) & (df["time"] <= end_utc + timedelta(days=1))]
    return df.reset_index(drop=True)


def _rates_to_df(rates):
    import pandas as pd

    df = pd.DataFrame(rates)
    df["time"] = pd.to_datetime(df["time"], unit="s", utc=True).dt.tz_localize(None)
    return df


def _is_stub_chunk(rates, req_start: datetime, req_end: datetime) -> bool:
    """Один бар-заглушка вне запрошенного окна (типичный ответ MT5 без истории)."""
    if rates is None or len(rates) == 0:
        return True
    if len(rates) >= 5:
        return False

    t0 = datetime.fromtimestamp(int(rates[0]["time"]), tz=timezone.utc).replace(tzinfo=None)
    t1 = datetime.fromtimestamp(int(rates[-1]["time"]), tz=timezone.utc).replace(tzinfo=None)
    # заглушка: все бары правее окна или левее «сейчас» далеко от запроса
    if t0 > req_end + timedelta(days=2) or t1 < req_start - timedelta(days=3650):
        return True
    if len(rates) == 1 and not (req_start - timedelta(days=2) <= t0 <= req_end + timedelta(days=2)):
        return True
    return False


def _merge_rate_frames(primary, extra):
    import pandas as pd

    if primary is None or len(primary) == 0:
        return extra
    if extra is None or len(extra) == 0:
        return primary
    df = (
        pd.concat([primary, extra], ignore_index=True)
        .drop_duplicates("time")
        .sort_values("time")
        .reset_index(drop=True)
    )
    return df


def _load_rates(mt5, broker, start_utc: datetime, tf_const, tf_name: str = "M1"):
    """Загрузка истории: API MT5 кусками + для M1 догрузка из локальных .hcc."""
    import pandas as pd
    import time

    mt5.symbol_select(broker, True)

    if start_utc.tzinfo is not None:
        start_utc = start_utc.astimezone(timezone.utc).replace(tzinfo=None)
    pad = timedelta(hours=2) if tf_name.upper().startswith("H") else timedelta(minutes=30)
    end = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(days=1)
    start = start_utc - pad
    tf_u = tf_name.upper()

    if tf_u == "M1":
        chunk = timedelta(days=14)
        warm_n = 500_000
        step_n = 80_000
    elif tf_u.startswith("M"):
        chunk = timedelta(days=90)
        warm_n = 100_000
        step_n = 50_000
    else:
        chunk = timedelta(days=800)
        warm_n = 50_000
        step_n = 20_000

    # прогрев локального кэша
    mt5.copy_rates_from_pos(broker, tf_const, 0, warm_n)

    parts = []
    cur = start
    while cur < end:
        nxt = min(cur + chunk, end)
        rates = mt5.copy_rates_range(broker, tf_const, cur, nxt)
        if _is_stub_chunk(rates, cur, nxt):
            time.sleep(0.15)
            rates = mt5.copy_rates_range(broker, tf_const, cur, nxt)
        if rates is not None and not _is_stub_chunk(rates, cur, nxt):
            parts.append(_rates_to_df(rates))
        cur = nxt

    if parts:
        df = pd.concat(parts, ignore_index=True)
    else:
        rates = mt5.copy_rates_from_pos(broker, tf_const, 0, warm_n)
        if rates is None or len(rates) == 0:
            df = None
        else:
            df = _rates_to_df(rates)

    if df is not None and len(df):
        df = df.drop_duplicates("time").sort_values("time").reset_index(drop=True)

        # догрузка назад через API, пока не упрёмся в MaxBars
        guard = 0
        while (
            len(df)
            and df["time"].iloc[0].to_pydatetime() > start + timedelta(hours=6)
            and guard < 80
        ):
            guard += 1
            edge = df["time"].iloc[0].to_pydatetime() - timedelta(minutes=1)
            rates = mt5.copy_rates_from(broker, tf_const, edge, step_n)
            if rates is None or len(rates) == 0:
                time.sleep(0.25)
                rates = mt5.copy_rates_from(broker, tf_const, edge, step_n)
            if rates is None or len(rates) == 0:
                break
            part = _rates_to_df(rates)
            new_min = part["time"].min().to_pydatetime()
            old_min = df["time"].iloc[0].to_pydatetime()
            if new_min >= old_min - timedelta(seconds=30):
                break
            df = _merge_rate_frames(df, part)
            print(
                f"      {tf_name} {broker}: API → {df['time'].iloc[0]}  bars={len(df)}",
                flush=True,
            )

    # M1: всегда догружаем .hcc (API брокера часто отдаёт только ~3 месяца)
    if tf_u == "M1":
        hcc_df = _load_rates_from_hcc(mt5, broker, start, end)
        if hcc_df is not None and len(hcc_df):
            before = 0 if df is None else len(df)
            df = _merge_rate_frames(df, hcc_df)
            print(
                f"      M1 {broker}: +hcc → {df['time'].iloc[0]} … {df['time'].iloc[-1]}  "
                f"bars={len(df)} (было {before})",
                flush=True,
            )

    return df


def _hit_both(side: str, open_: float, high: float, low: float, sl: float, tp: float) -> str:
    """Если в одном баре и SL и TP — ближе к open бара."""
    if side == "BUY":
        hit_sl, hit_tp = low <= sl, high >= tp
        if hit_sl and hit_tp:
            return "SL" if abs(open_ - sl) <= abs(open_ - tp) else "TP"
        if hit_sl:
            return "SL"
        if hit_tp:
            return "TP"
    else:
        hit_sl, hit_tp = high >= sl, low <= tp
        if hit_sl and hit_tp:
            return "SL" if abs(open_ - sl) <= abs(open_ - tp) else "TP"
        if hit_sl:
            return "SL"
        if hit_tp:
            return "TP"
    return ""


def simulate_leg(
    df,
    side: str,
    entry: float,
    sl: float,
    tp: float,
    signal_utc: datetime,
    order_type: str,
    cancel_at: datetime | None = None,
    force_close_at: datetime | None = None,
    sl_mods: list[tuple[datetime, float]] | None = None,
    level_mods: list[tuple[datetime, float | None, float | None, float | None]] | None = None,
):
    """Симуляция одной сделки с отменой лимита, сменой уровней/SL и принудительным закрытием."""
    import pandas as pd

    if signal_utc.tzinfo is not None:
        signal_utc = signal_utc.replace(tzinfo=None)
    work = df[df["time"] >= pd.Timestamp(signal_utc) - pd.Timedelta(minutes=5)].reset_index(drop=True)
    if work.empty:
        return "NO_DATA", 0.0, 0.0, 0, "empty", sl, sl

    open_price = 0.0
    start_i = 0
    cur_entry = entry
    cur_sl = sl
    cur_tp = tp
    mod_i = 0
    mods = sorted(sl_mods or [], key=lambda x: x[0])
    lvl_i = 0
    lvls = sorted(level_mods or [], key=lambda x: x[0])

    def apply_mods_until(t, *, pending: bool):
        nonlocal cur_sl, cur_entry, cur_tp, mod_i, lvl_i
        while lvl_i < len(lvls) and lvls[lvl_i][0] <= t:
            e, s, tpv = lvls[lvl_i][1], lvls[lvl_i][2], lvls[lvl_i][3]
            if pending and e is not None and e > 0:
                cur_entry = e
            if s is not None and s > 0:
                cur_sl = s
            if tpv is not None and tpv > 0:
                cur_tp = tpv
            lvl_i += 1
        while mod_i < len(mods) and mods[mod_i][0] <= t:
            if mods[mod_i][1] > 0:
                cur_sl = mods[mod_i][1]
            mod_i += 1

    if order_type == "limit":
        filled = False
        for i, row in work.iterrows():
            t = row["time"].to_pydatetime() if hasattr(row["time"], "to_pydatetime") else row["time"]
            apply_mods_until(t, pending=True)
            if cancel_at and t >= cancel_at:
                return "CANCELLED", 0.0, 0.0, int(i) + 1, "limit cancelled", cur_sl, cur_sl
            high, low = float(row["high"]), float(row["low"])
            if low <= cur_entry <= high:
                open_price = cur_entry
                start_i = int(i)
                filled = True
                break
        if not filled:
            return "EXPIRED", 0.0, 0.0, len(work), "limit not filled", cur_sl, cur_sl
    else:
        # market: учесть смену уровней, если пришла в ту же секунду
        t0 = work.iloc[0]["time"]
        t0 = t0.to_pydatetime() if hasattr(t0, "to_pydatetime") else t0
        apply_mods_until(t0, pending=True)
        mid = float(work.iloc[0]["open"])
        open_price = mid
        start_i = 0

    risk_sl_at_open = cur_sl

    for i in range(start_i, len(work)):
        row = work.iloc[i]
        t = row["time"].to_pydatetime() if hasattr(row["time"], "to_pydatetime") else row["time"]
        apply_mods_until(t, pending=False)
        high, low = float(row["high"]), float(row["low"])
        o = float(row["open"])
        close = float(row["close"])

        if force_close_at and t >= force_close_at:
            return "MANUAL", open_price, close, i - start_i + 1, "manage close", cur_sl, risk_sl_at_open

        which = _hit_both(side, o, high, low, cur_sl, cur_tp)
        if which == "SL":
            return "SL", open_price, cur_sl, i - start_i + 1, "", cur_sl, risk_sl_at_open
        if which == "TP":
            return "TP", open_price, cur_tp, i - start_i + 1, "", cur_sl, risk_sl_at_open

    return (
        "EXPIRED",
        open_price,
        float(work.iloc[-1]["close"]),
        len(work) - start_i,
        "no hit",
        cur_sl,
        risk_sl_at_open,
    )


def _parse_mt5_time(s: str) -> datetime:
    s = (s or "").strip()
    for fmt in ("%Y.%m.%d %H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    raise ValueError(s)


def load_timeline_rows() -> list[dict]:
    """Читает timeline из mt5/signals.csv (с колонкой action) или timeline_latest.csv."""
    local = cfg.OUTPUT_DIR / "mt5" / "signals.csv"
    common = cfg.mt5_common_files()
    path = local
    if common:
        cpath = common / "SignalKit" / "signals.csv"
        if cpath.exists():
            path = cpath
    if not path.exists() and local.exists():
        path = local

    # предпочитаем output/timeline_latest.csv (богаче)
    tl = cfg.OUTPUT_DIR / "timeline_latest.csv"
    if tl.exists():
        rows = list(csv.DictReader(tl.open(encoding="utf-8-sig")))
        if rows:
            return rows

    if not path.exists():
        raise FileNotFoundError("Нет signals.csv / timeline — сначала «Скачать сигналы»")

    raw = list(csv.DictReader(path.open(encoding="utf-8"), delimiter=";"))
    if not raw:
        return []
    # старый формат без action → каждый ряд = open
    if "action" not in raw[0]:
        out = []
        for r in raw:
            out.append(
                {
                    "chain_id": r.get("msg_id", ""),
                    "root_id": r.get("msg_id", ""),
                    "msg_id": r.get("msg_id", ""),
                    "time_utc": r.get("time", "").replace(".", "-") if False else r.get("time", ""),
                    "symbol": r.get("symbol", ""),
                    "action": "open",
                    "side": r.get("side", ""),
                    "order_type": r.get("order_type", "market"),
                    "entry": r.get("entry", "0"),
                    "sl": r.get("sl", "0"),
                    "tp": r.get("tp", "0"),
                    "tp_open": r.get("tp_open", "0"),
                    "parent_id": "0",
                    "note": "",
                }
            )
            # нормализуем время в ISO-like
            t = out[-1]["time_utc"]
            if "." in t[:10]:
                # 2025.08.04 06:55:37 → keep, parser handles
                pass
            out[-1]["time_utc"] = t
        return out

    # mt5 signals with action — time is Y.m.d
    out = []
    for r in raw:
        out.append(
            {
                "chain_id": r.get("chain_id") or r.get("msg_id", ""),
                "root_id": r.get("root_id") or r.get("msg_id", ""),
                "msg_id": r.get("msg_id", ""),
                "time_utc": r.get("time", ""),
                "symbol": r.get("symbol", ""),
                "action": r.get("action", "open"),
                "side": r.get("side", ""),
                "order_type": r.get("order_type", "market"),
                "entry": r.get("entry", "0"),
                "sl": r.get("sl", "0"),
                "tp": r.get("tp", "0"),
                "tp_open": r.get("tp_open", "0"),
                "parent_id": r.get("parent_id", "0"),
                "note": "",
            }
        )
    return out


def run_backtest() -> dict:
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    if hasattr(sys.stderr, "reconfigure"):
        try:
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    print("Анализ: импорт MetaTrader5…", flush=True)
    import MetaTrader5 as mt5

    cp = cfg.load_settings()
    tf_name = backtest_timeframe(cp)
    tf_const = mt5_timeframe(mt5, tf_name)

    print("Анализ: читаю signals / timeline…", flush=True)
    rows = load_timeline_rows()
    print(f"Timeline events: {len(rows)} | TF={tf_name}", flush=True)
    if not rows:
        raise RuntimeError("Timeline пуст — сначала «1. Скачать сигналы»")

    print("Анализ: подключение к MT5 (терминал должен быть открыт)…", flush=True)
    if not mt5.initialize():
        raise RuntimeError(f"MT5 не открыт / не залогинен: {mt5.last_error()}")
    acc = mt5.account_info()
    if acc is None:
        mt5.shutdown()
        raise RuntimeError("MT5 открыт, но нет входа в счёт — залогиньтесь и повторите")
    print(f"Анализ: MT5 OK — {acc.server} / login {acc.login}", flush=True)

    # group by chain
    chains: dict[str, list[dict]] = {}
    for r in rows:
        cid = str(r.get("chain_id") or r.get("msg_id") or "")
        chains.setdefault(cid, []).append(r)
    for cid in chains:
        chains[cid].sort(key=lambda x: (_parse_mt5_time(x["time_utc"]), int(x.get("msg_id") or 0)))

    earliest: dict[str, datetime] = {}
    for cid, evs in chains.items():
        sym = evs[0]["symbol"]
        t0 = _parse_mt5_time(evs[0]["time_utc"])
        if sym not in earliest or t0 < earliest[sym]:
            earliest[sym] = t0

    # без достаточного MaxBars / при блокировке .hcc API отдаёт мало M1
    if earliest and tf_name.upper() == "M1":
        global_earliest = min(earliest.values())
        need = max(1_000_000, _bars_needed_for_span(global_earliest, tf_name))
        ensure_mt5_maxbars(mt5, need)

    results: list[TradeResult] = []
    cache: dict[str, str | None] = {}
    rates_cache: dict[str, object] = {}
    # полнота истории по брокеру-символу
    history_cov: dict[str, dict] = {}
    n_chains = 0

    # заранее резолвим символы и копируем .hcc (один перезапуск MT5 при блокировке)
    for cid, evs in chains.items():
        sym = evs[0]["symbol"]
        if sym not in cache:
            cache[sym] = resolve_symbol(mt5, sym)
    if tf_name.upper() == "M1" and earliest:
        brokers = sorted({b for b in cache.values() if b})
        global_earliest = min(earliest.values())
        _prefetch_hcc_for_symbols(
            mt5,
            brokers,
            global_earliest - timedelta(days=1),
            datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(days=1),
        )

    for cid, evs in chains.items():
        n_chains += 1
        sym = evs[0]["symbol"]
        if sym not in cache:
            cache[sym] = resolve_symbol(mt5, sym)
        broker = cache[sym]
        if not broker:
            for e in evs:
                if e.get("action") == "open":
                    results.append(
                        TradeResult(
                            e["msg_id"], e["time_utc"], sym, "", e.get("side", ""),
                            e.get("order_type", ""), float(e.get("entry") or 0),
                            float(e.get("sl") or 0), float(e.get("tp") or 0),
                            0, 0, "NO_SYMBOL", 0, 0, "", cid, str(e.get("root_id") or cid),
                        )
                    )
            continue

        if broker not in rates_cache:
            print(f"  загрузка {tf_name} {broker} …", flush=True)
            m1df = _load_rates(mt5, broker, earliest[sym], tf_const, tf_name)
            rates_cache[broker] = {"primary": m1df}
            need = earliest[sym]
            if m1df is not None and len(m1df):
                tmin = m1df["time"].iloc[0].to_pydatetime()
                tmax = m1df["time"].iloc[-1].to_pydatetime()
                gap_days = max(0, (tmin - need).total_seconds() / 86400.0)
                complete = gap_days <= 2.0
                cov_pct = 100.0
                if not complete:
                    span_need = max(1.0, (datetime.now(timezone.utc).replace(tzinfo=None) - need).total_seconds() / 86400.0)
                    covered_from_need = max(0.0, (tmax - max(tmin, need)).total_seconds() / 86400.0)
                    cov_pct = round(min(100.0, 100.0 * covered_from_need / span_need), 1)
                history_cov[broker] = {
                    "symbol": broker,
                    "needed_from": need.strftime("%Y-%m-%d"),
                    "actual_from": tmin.strftime("%Y-%m-%d"),
                    "actual_to": tmax.strftime("%Y-%m-%d"),
                    "bars": int(len(m1df)),
                    "gap_days": round(gap_days, 1),
                    "coverage_pct": cov_pct if not complete else 100.0,
                    "complete": complete,
                }
                print(f"    bars={len(m1df)}  {tmin} … {tmax}", flush=True)
                if not complete:
                    print(
                        f"    ! {tf_name} только с {tmin.date()} (нужно с {need.date()}) "
                        f"— покрытие ~{history_cov[broker]['coverage_pct']}%",
                        flush=True,
                    )
            else:
                history_cov[broker] = {
                    "symbol": broker,
                    "needed_from": need.strftime("%Y-%m-%d"),
                    "actual_from": None,
                    "actual_to": None,
                    "bars": 0,
                    "gap_days": None,
                    "coverage_pct": 0.0,
                    "complete": False,
                }
                print(f"    {tf_name} пусто для {broker}", flush=True)

        pack = rates_cache[broker]
        primary = pack["primary"]

        open_idx = [i for i, e in enumerate(evs) if (e.get("action") or "").lower() == "open"]
        if not open_idx:
            continue

        for k, idx in enumerate(open_idx):
            e = evs[idx]
            next_boundary = open_idx[k + 1] if k + 1 < len(open_idx) else len(evs)
            between = evs[idx + 1 : next_boundary]

            side = (e.get("side") or "").upper()
            entry = float(e.get("entry") or 0)
            sl = float(e.get("sl") or 0)
            tp = float(e.get("tp") or 0)
            order_type = (e.get("order_type") or "market").lower()
            open_t = _parse_mt5_time(e["time_utc"])
            root = str(e.get("root_id") or cid)

            # только выбранный TF (M1) — без fallback на H1
            df = primary
            used_tf = tf_name
            if primary is None or len(primary) == 0:
                results.append(
                    TradeResult(
                        e["msg_id"], e["time_utc"], sym, broker, side, order_type,
                        entry, sl, tp, 0, 0, "NO_DATA", 0, 0, f"no {tf_name}", cid, root,
                    )
                )
                continue
            p0 = primary["time"].iloc[0].to_pydatetime()
            if open_t < p0 - timedelta(minutes=30):
                results.append(
                    TradeResult(
                        e["msg_id"], e["time_utc"], sym, broker, side, order_type,
                        entry, sl, tp, 0, 0, "NO_DATA", 0, 0,
                        f"no {tf_name} before {p0.date()}", cid, root,
                    )
                )
                continue

            if df is None or len(df) == 0:
                results.append(
                    TradeResult(
                        e["msg_id"], e["time_utc"], sym, broker, side, order_type,
                        entry, sl, tp, 0, 0, "NO_DATA", 0, 0, f"no {used_tf}", cid, root,
                    )
                )
                continue

            # рынок без цены в тексте — берём open ближайшего бара
            if order_type == "market" and entry <= 0:
                import pandas as pd

                work0 = df[df["time"] >= pd.Timestamp(open_t) - pd.Timedelta(minutes=5)]
                if len(work0):
                    entry = float(work0.iloc[0]["open"])
                    if tp <= 0 and sl > 0:
                        from .parser import synth_tp

                        tp = synth_tp(side, entry, sl, 2.0)

            if not direction_ok(side, entry, sl, tp):
                results.append(
                    TradeResult(
                        e["msg_id"], e["time_utc"], sym, broker, side, order_type,
                        entry, sl, tp, 0, 0, "BAD_LEVELS", 0, 0, "", cid, root,
                    )
                )
                continue

            cancel_at = None
            force_close_at = None
            sl_mods: list[tuple[datetime, float]] = []
            level_mods: list[tuple[datetime, float | None, float | None, float | None]] = []
            for nxt in between:
                nact = (nxt.get("action") or "").lower()
                nt = _parse_mt5_time(nxt["time_utc"])
                if nact == "cancel_pending":
                    cancel_at = nt
                elif nact == "modify_sl":
                    sl_mods.append((nt, float(nxt.get("sl") or 0)))
                elif nact == "modify_levels":
                    level_mods.append(
                        (
                            nt,
                            float(nxt.get("entry") or 0) or None,
                            float(nxt.get("sl") or 0) or None,
                            float(nxt.get("tp") or 0) or None,
                        )
                    )
                elif nact == "close":
                    force_close_at = nt

            if force_close_at is None and k + 1 < len(open_idx):
                force_close_at = _parse_mt5_time(evs[open_idx[k + 1]]["time_utc"])

            outcome, opx, cpx, bars, note, final_sl, risk_sl = simulate_leg(
                df, side, entry, sl, tp, open_t, order_type,
                cancel_at=cancel_at,
                force_close_at=force_close_at,
                sl_mods=sl_mods,
                level_mods=level_mods,
            )
            if used_tf != tf_name and note:
                note = f"{note}|{used_tf}"
            elif used_tf != tf_name:
                note = used_tf
            pnl = (
                pnl_R(side, opx, cpx, risk_sl)
                if outcome in ("TP", "SL", "EXPIRED", "MANUAL") and opx
                else 0.0
            )
            results.append(
                TradeResult(
                    e["msg_id"], e["time_utc"], sym, broker, side, order_type,
                    entry, risk_sl if risk_sl else sl, tp, opx, cpx, outcome, pnl, bars, note, cid, root,
                )
            )

        if n_chains % 20 == 0:
            print(f"  ... chains {n_chains}/{len(chains)}", flush=True)

    mt5.shutdown()

    out_dir = cfg.OUTPUT_DIR / "backtest"
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    fields = list(asdict(results[0]).keys()) if results else []
    latest = out_dir / "backtest_latest.csv"
    with (out_dir / f"backtest_{stamp}.csv").open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for row in results:
            w.writerow(asdict(row))
    latest.write_text((out_dir / f"backtest_{stamp}.csv").read_text(encoding="utf-8-sig"), encoding="utf-8-sig")

    import json
    import pandas as pd

    df = pd.DataFrame([asdict(r) for r in results])
    tp_n = int((df["outcome"] == "TP").sum()) if len(df) else 0
    sl_n = int((df["outcome"] == "SL").sum()) if len(df) else 0
    man_n = int((df["outcome"] == "MANUAL").sum()) if len(df) else 0
    can_n = int((df["outcome"] == "CANCELLED").sum()) if len(df) else 0
    nodata = int((df["outcome"] == "NO_DATA").sum()) if len(df) else 0
    nosym = int((df["outcome"] == "NO_SYMBOL").sum()) if len(df) else 0
    n_total = int(len(df))
    traded = df[df["outcome"].isin(["TP", "SL", "MANUAL"])] if len(df) else df
    sum_r = float(traded["pnl_R"].sum()) if len(traded) else 0.0
    avg_r = float(traded["pnl_R"].mean()) if len(traded) else 0.0
    wr = round(tp_n / (tp_n + sl_n) * 100, 1) if tp_n + sl_n else 0.0

    # --- качество / полнота истории ---
    simulatable = max(0, n_total - nodata - nosym)
    trade_coverage_pct = round(100.0 * simulatable / n_total, 1) if n_total else 0.0
    cov_list = list(history_cov.values())
    incomplete_syms = [c for c in cov_list if not c.get("complete")]
    if cov_list:
        # среднее покрытие символов, взвешенное числом сделок по символу
        weights = []
        for c in cov_list:
            w = int((df["broker_symbol"] == c["symbol"]).sum()) if len(df) else 1
            weights.append(max(1, w))
        hist_avg = round(
            sum(float(c["coverage_pct"]) * w for c, w in zip(cov_list, weights)) / sum(weights),
            1,
        )
    else:
        hist_avg = 0.0
    # итоговый балл: нельзя получить высокий score при куче NO_DATA
    quality_score = round(0.55 * trade_coverage_pct + 0.45 * hist_avg, 1)
    if quality_score >= 95 and nodata == 0 and not incomplete_syms:
        quality_status = "FULL"
        quality_label = "ПОЛНАЯ"
    elif quality_score >= 70:
        quality_status = "PARTIAL"
        quality_label = "ЧАСТИЧНАЯ"
    else:
        quality_status = "POOR"
        quality_label = "СЛАБАЯ"

    quality = {
        "quality_score": quality_score,
        "quality_status": quality_status,
        "quality_label": quality_label,
        "trade_coverage_pct": trade_coverage_pct,
        "history_avg_pct": hist_avg,
        "n_total": n_total,
        "n_simulated": simulatable,
        "n_no_data": nodata,
        "n_no_symbol": nosym,
        "symbols_total": len(cov_list),
        "symbols_incomplete": len(incomplete_syms),
        "timeframe": tf_name,
        "sum_R_on_simulated": round(sum_r, 2),
        "reliable": quality_status == "FULL",
        "symbols": sorted(cov_list, key=lambda x: float(x.get("coverage_pct") or 0)),
        "hint": (
            "История MT5 полная — цифры TP/SL/R можно сравнивать между прогонами."
            if quality_status == "FULL"
            else (
                "История НЕ полная: часть сделок без баров (NO_DATA) или обрезана дата начала. "
                "Сумма R только по просчитанным сделкам — на другом ПК с полной историей итог будет другим. "
                "Откройте графики проблемных символов в MT5, прокрутите историю назад, "
                "MaxBars=Unlimited, перезапустите MT5 и повторите анализ."
            )
        ),
    }
    (out_dir / "history_quality.json").write_text(
        json.dumps(quality, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    lines = [
        f"# Анализ сделок (бэктест {tf_name} + сопровождение)",
        "",
        f"## Полнота истории: **{quality_score}%** — {quality_label}",
        "",
        f"- Статус: **{quality_status}** ({'можно доверять цифрам' if quality['reliable'] else 'цифры НЕ полные — не сравнивайте с другим ПК'})",
        f"- Покрытие сделок (есть история): **{trade_coverage_pct}%** ({simulatable}/{n_total})",
        f"- Среднее покрытие символов по датам: **{hist_avg}%**",
        f"- Без истории NO_DATA: **{nodata}** | Нет символа: **{nosym}** | Символов с дырами: **{len(incomplete_syms)}/{len(cov_list)}**",
        f"- {quality['hint']}",
        "",
        f"- Таймфрейм: **{tf_name}**",
        f"- Сделок: **{n_total}** | Цепочек: **{len(chains)}**",
        f"- TP: **{tp_n}** | SL: **{sl_n}** | MANUAL: **{man_n}** | CANCELLED: **{can_n}**",
        f"- Winrate (TP/(TP+SL)): **{wr}%**",
        f"- Сумма (только просчитанные): **{sum_r:.1f}R** | Средняя: **{avg_r:.2f}R**",
        "",
        "## История по символам",
        "",
        "| Symbol | Нужно с | Факт с | Баров | Покрытие % | ОК",
        "|---|---|---|---:|---:|---|",
    ]
    for c in sorted(cov_list, key=lambda x: x["symbol"]):
        ok = "OK" if c.get("complete") else "ДЫРА"
        lines.append(
            f"| {c['symbol']} | {c.get('needed_from')} | {c.get('actual_from') or '—'} | "
            f"{c.get('bars', 0)} | {c.get('coverage_pct', 0)} | {ok} |"
        )
    lines += [
        "",
        "## По символам (результаты)",
        "",
        "| Symbol | N | TP | SL | WR% | Sum R |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    if len(df):
        for sym, g in df.groupby("csv_symbol"):
            tpn = int((g["outcome"] == "TP").sum())
            sln = int((g["outcome"] == "SL").sum())
            wrn = round(tpn / (tpn + sln) * 100) if tpn + sln else 0
            gtr = g[g["outcome"].isin(["TP", "SL", "MANUAL"])]
            lines.append(
                f"| {sym} | {len(g)} | {tpn} | {sln} | {wrn} | {float(gtr['pnl_R'].sum()):.1f} |"
            )
    (out_dir / "backtest_latest.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(
        f"Готово [{tf_name}]: TP={tp_n} SL={sl_n} MANUAL={man_n} CANCEL={can_n} "
        f"NO_DATA={nodata} sum={sum_r:.1f}R → {out_dir}",
        flush=True,
    )
    print(
        f"ПОЛНОТА ИСТОРИИ: {quality_score}% ({quality_label}) | "
        f"сделок с барами {trade_coverage_pct}% | символов с дырами {len(incomplete_syms)}/{len(cov_list)}",
        flush=True,
    )
    if not quality["reliable"]:
        print(f"! {quality['hint']}", flush=True)
    return {
        "ok": True,
        "n": len(results),
        "tp": tp_n,
        "sl": sl_n,
        "manual": man_n,
        "cancelled": can_n,
        "no_data": nodata,
        "sum_R": round(sum_r, 2),
        "winrate": wr,
        "chains": len(chains),
        "timeframe": tf_name,
        "quality_score": quality_score,
        "quality_status": quality_status,
        "trade_coverage_pct": trade_coverage_pct,
        "reliable": quality["reliable"],
    }
