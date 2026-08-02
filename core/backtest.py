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


def _load_rates(mt5, broker, start_utc: datetime, tf_const, tf_name: str = "M1"):
    """Загрузка истории; для M1 — кусками (у MT5 лимит на большой диапазон)."""
    import pandas as pd

    if start_utc.tzinfo is not None:
        start_utc = start_utc.astimezone(timezone.utc).replace(tzinfo=None)
    pad = timedelta(hours=2) if tf_name.upper().startswith("H") else timedelta(minutes=30)
    end = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(days=1)
    start = start_utc - pad

    # размер куска: M1 ~30 дней, H1 — целиком
    if tf_name.upper() == "M1":
        chunk = timedelta(days=25)
    elif tf_name.upper().startswith("M"):
        chunk = timedelta(days=120)
    else:
        chunk = timedelta(days=800)

    parts = []
    cur = start
    while cur < end:
        nxt = min(cur + chunk, end)
        rates = mt5.copy_rates_range(broker, tf_const, cur, nxt)
        # отбрасываем «пустые» ответы (1 бар-заглушка без реальной истории)
        if rates is not None and len(rates) >= 50:
            parts.append(pd.DataFrame(rates))
        cur = nxt

    if not parts:
        # запасной вариант — последние N баров
        n = 100000 if tf_name.upper() == "M1" else 10000
        rates = mt5.copy_rates_from_pos(broker, tf_const, 0, n)
        if rates is None or len(rates) == 0:
            return None
        df = pd.DataFrame(rates)
    else:
        df = pd.concat(parts, ignore_index=True)

    df["time"] = pd.to_datetime(df["time"], unit="s", utc=True).dt.tz_localize(None)
    df = df.drop_duplicates("time").sort_values("time").reset_index(drop=True)
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
        sys.stdout.reconfigure(errors="replace")

    import MetaTrader5 as mt5

    cp = cfg.load_settings()
    tf_name = backtest_timeframe(cp)
    tf_const = mt5_timeframe(mt5, tf_name)

    rows = load_timeline_rows()
    print(f"Timeline events: {len(rows)} | TF={tf_name}")

    if not mt5.initialize():
        raise RuntimeError(f"MT5 не открыт / не залогинен: {mt5.last_error()}")

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

    results: list[TradeResult] = []
    cache: dict[str, str | None] = {}
    rates_cache: dict[str, object] = {}
    n_chains = 0

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
            rates_cache[broker] = {"primary": m1df, "fallback": None}
            if m1df is not None and len(m1df):
                tmin = m1df["time"].iloc[0]
                tmax = m1df["time"].iloc[-1]
                print(f"    bars={len(m1df)}  {tmin} … {tmax}", flush=True)
                # если M1 не покрывает ранние сигналы — подгружаем H1
                if tmin.to_pydatetime() > earliest[sym] + timedelta(days=2):
                    print(f"    + fallback H1 (M1 с {tmin.date()})", flush=True)
                    rates_cache[broker]["fallback"] = _load_rates(
                        mt5, broker, earliest[sym], mt5.TIMEFRAME_H1, "H1"
                    )
            else:
                print(f"    {tf_name} пусто → H1", flush=True)
                rates_cache[broker]["fallback"] = _load_rates(
                    mt5, broker, earliest[sym], mt5.TIMEFRAME_H1, "H1"
                )

        pack = rates_cache[broker]
        primary = pack["primary"]
        fallback = pack["fallback"]

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

            # выбираем TF: M1 если сигнал внутри покрытия, иначе H1
            df = primary
            used_tf = tf_name
            if primary is None or len(primary) == 0:
                df = fallback
                used_tf = "H1"
            else:
                p0 = primary["time"].iloc[0].to_pydatetime()
                if open_t < p0 - timedelta(minutes=30):
                    if fallback is None:
                        fallback = _load_rates(mt5, broker, earliest[sym], mt5.TIMEFRAME_H1, "H1")
                        pack["fallback"] = fallback
                    df = fallback
                    used_tf = "H1"

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
            print(f"  ... chains {n_chains}/{len(chains)}")

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

    import pandas as pd

    df = pd.DataFrame([asdict(r) for r in results])
    tp_n = int((df["outcome"] == "TP").sum()) if len(df) else 0
    sl_n = int((df["outcome"] == "SL").sum()) if len(df) else 0
    man_n = int((df["outcome"] == "MANUAL").sum()) if len(df) else 0
    can_n = int((df["outcome"] == "CANCELLED").sum()) if len(df) else 0
    nodata = int((df["outcome"] == "NO_DATA").sum()) if len(df) else 0
    traded = df[df["outcome"].isin(["TP", "SL", "MANUAL"])] if len(df) else df
    sum_r = float(traded["pnl_R"].sum()) if len(traded) else 0.0
    avg_r = float(traded["pnl_R"].mean()) if len(traded) else 0.0
    wr = round(tp_n / (tp_n + sl_n) * 100, 1) if tp_n + sl_n else 0.0

    lines = [
        f"# Анализ сделок (бэктест {tf_name} + сопровождение)",
        "",
        f"- Таймфрейм: **{tf_name}** (ранние сделки без M1 — fallback H1)",
        f"- Сделок: **{len(df)}** | Цепочек: **{len(chains)}**",
        f"- TP: **{tp_n}** | SL: **{sl_n}** | MANUAL: **{man_n}** | CANCELLED: **{can_n}**",
        f"- Winrate (TP/(TP+SL)): **{wr}%**",
        f"- Сумма: **{sum_r:.1f}R** | Средняя: **{avg_r:.2f}R**",
        f"- NO_SYMBOL: **{int((df['outcome']=='NO_SYMBOL').sum()) if len(df) else 0}**",
        f"- NO_DATA: **{nodata}**",
        "",
        "## По символам",
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
        f"NO_DATA={nodata} sum={sum_r:.1f}R → {out_dir}"
    )
    return {
        "ok": True,
        "n": len(results),
        "tp": tp_n,
        "sl": sl_n,
        "manual": man_n,
        "cancelled": can_n,
        "sum_R": round(sum_r, 2),
        "winrate": wr,
        "chains": len(chains),
        "timeframe": tf_name,
    }
