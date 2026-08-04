# -*- coding: utf-8 -*-
"""Универсальный парсер сигналов Telegram по правилам из НАСТРОЙКИ.ini"""

from __future__ import annotations

import asyncio
import csv
import html as html_lib
import json
import re
import sys
import time
import urllib.request
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import config as cfg

MESSAGE_BLOCK_RE = re.compile(
    r'class="tgme_widget_message[^"]*"[^>]*?data-post="(?P<channel>[^/]+)/(?P<id>\d+)"',
    re.IGNORECASE,
)
DATETIME_RE = re.compile(r'datetime="(?P<dt>[^"]+)"')
TEXT_RE = re.compile(
    r'class="tgme_widget_message_text[^"]*"[^>]*>(?P<body>.*?)</div>',
    re.IGNORECASE | re.DOTALL,
)
TAG_RE = re.compile(r"<[^>]+>")
SYMBOL_HASH_RE = re.compile(r"#\s*([A-Za-z]{3,12})\b")


@dataclass
class ParsedSignal:
    message_id: int
    date_utc: str
    link: str
    raw_text: str
    symbol: str
    side: str
    order_type: str
    entry: float
    sl: float
    tp: float
    tp_open: bool
    timeframe: str
    parse_ok: bool
    parse_note: str = ""
    source: str = ""
    role: str = ""  # signal | manage | noise
    action: str = ""
    chain_id: int = 0
    parent_id: int = 0
    formal: bool = False
    inherit_levels: bool = False  # SL/TP взять из активной цепочки


@dataclass
class ParseRules:
    fmt: str  # labels | compact
    must_contain: list[str]
    skip_if_contains: list[str]
    label_side: str
    label_entry: str
    label_sl: str
    label_tp: str
    buy_words: list[str]
    sell_words: list[str]
    limit_words: list[str]
    tp_open_words: list[str]
    open_tp_rr: float
    symbol_from_hashtag: bool
    compact_side_buy: str
    compact_side_sell: str
    compact_sl_word: str
    compact_tp_word: str
    manage_enabled: bool = False
    manage_trigger_words: list[str] = field(default_factory=list)
    inherit_words: list[str] = field(default_factory=list)


def _split_list(s: str) -> list[str]:
    if not s:
        return []
    parts = re.split(r"[|;\n]+", s)
    return [p.strip() for p in parts if p.strip()]


def rules_from_settings(cp) -> ParseRules:
    from .manage import manage_from_settings

    mr = manage_from_settings(cp)
    inherit = _split_list(
        cfg.get(
            cp,
            "manage",
            "words_inherit_levels",
            "не меняем|без изменений|параметры без|остальные параметры|"
            "keep sl|sl unchanged|levels unchanged|same stop",
        )
    )
    triggers = (
        mr.words_cancel_pending
        + mr.words_reverse
        + mr.words_breakeven
        + mr.words_close
        + mr.words_modify_sl
        + mr.words_modify_tp
        + mr.words_clear_expiry
        + mr.words_add
        + mr.words_to_market
        + inherit
        + [
            "цена открытия",
            "текущая цена",
            "открываемся",
            "заходим по рынку",
            "открываем по рынку",
            "стоп лосс",
            "stop loss",
            "тейк профит",
            "take profit",
            "entry",
        ]
    )
    return ParseRules(
        fmt=cfg.get(cp, "parse", "format", "labels").lower(),
        must_contain=_split_list(cfg.get(cp, "parse", "must_contain")),
        skip_if_contains=_split_list(cfg.get(cp, "parse", "skip_if_contains")),
        label_side=cfg.get(cp, "parse", "label_side", "Тип сделки"),
        label_entry=cfg.get(cp, "parse", "label_entry", "Цена открытия"),
        label_sl=cfg.get(cp, "parse", "label_sl", "Стоп лосс"),
        label_tp=cfg.get(cp, "parse", "label_tp", "Тейк профит"),
        buy_words=_split_list(cfg.get(cp, "parse", "buy_words", "покуп|лонг|buy")),
        sell_words=_split_list(cfg.get(cp, "parse", "sell_words", "продаж|шорт|sell")),
        limit_words=_split_list(cfg.get(cp, "parse", "limit_words", "лимит|limit")),
        tp_open_words=_split_list(cfg.get(cp, "parse", "tp_open_words", "открыт")),
        open_tp_rr=cfg.get_float(cp, "parse", "open_tp_rr", 2.0),
        symbol_from_hashtag=cfg.get(cp, "parse", "symbol_from_hashtag", "yes").lower()
        in ("yes", "1", "true", "да"),
        compact_side_buy=cfg.get(cp, "parse", "compact_side_buy", "Buy"),
        compact_side_sell=cfg.get(cp, "parse", "compact_side_sell", "Sell"),
        compact_sl_word=cfg.get(cp, "parse", "compact_sl_word", "SL"),
        compact_tp_word=cfg.get(cp, "parse", "compact_tp_word", "TP"),
        manage_enabled=mr.enabled,
        manage_trigger_words=triggers,
        inherit_words=inherit,
    )


def parse_price(raw: str) -> float:
    s = (raw or "").strip().replace("\xa0", "").replace(" ", "")
    s = s.strip(",.;:")
    if not s:
        return 0.0
    if "," in s and "." in s:
        if s.rfind(",") > s.rfind("."):
            s = s.replace(".", "").replace(",", ".")
        else:
            s = s.replace(",", "")
    elif "," in s:
        s = s.replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return 0.0


def first_price_in(raw: str) -> float:
    """Берёт первое число из строки (для «1,13860, стоп лосс …»)."""
    m = re.search(r"\d{1,6}(?:[.,]\d{1,6})?", raw or "")
    return parse_price(m.group(0)) if m else 0.0


def html_to_text(body: str) -> str:
    body = re.sub(r"<br\s*/?>", "\n", body, flags=re.IGNORECASE)
    body = re.sub(r"</p>", "\n", body, flags=re.IGNORECASE)
    body = TAG_RE.sub("", body)
    return html_lib.unescape(body).strip()


def _contains_any(text_l: str, words: list[str]) -> bool:
    return any(w.lower() in text_l for w in words if w)


def _extract_after_label(text: str, label: str) -> str:
    if not label:
        return ""
    # ищем метку без учёта регистра
    m = re.search(re.escape(label) + r"\s*:?\s*", text, re.IGNORECASE)
    if not m:
        return ""
    rest = text[m.end() :]
    # до конца строки
    line = rest.splitlines()[0] if rest else ""
    # убрать маркеры списка
    line = re.sub(r"^[▪️•\-\s]+", "", line)
    return line.strip()


_PRICE_RE = r"(\d{1,6}(?:[.,]\d{1,6})?)"


def normalize_signal_text(text: str) -> str:
    """Нормализация опечаток / вариантов написания (канал-агностично)."""
    t = text or ""
    reps = (
        (r"стот\s*лосс", "стоп лосс"),
        (r"стоп\s*лос\b", "стоп лосс"),
        (r"stop[\s\-]*loss", "стоп лосс"),
        (r"take[\s\-]*profit", "тейк профит"),
        (r"\bтп\b", "тейк профит"),
        (r"\bсл\b", "стоп лосс"),
    )
    for pat, repl in reps:
        t = re.sub(pat, repl, t, flags=re.IGNORECASE)
    return t


def levels_inherited(text: str, inherit_words: list[str] | None = None) -> bool:
    tl = (text or "").lower()
    words = inherit_words or []
    if words and _contains_any(tl, words):
        return True
    return bool(
        re.search(
            r"не\s+меняем|без\s+изменен|параметры\s+без|остальные\s+параметры|"
            r"keep\s+(?:sl|tp|levels|params)|sl\s+unchanged|same\s+stop|"
            r"levels?\s+unchanged|params?\s+unchanged",
            tl,
        )
    )


def infer_side_from_levels(entry: float, sl: float) -> str:
    if entry <= 0 or sl <= 0 or abs(entry - sl) < 1e-12:
        return ""
    if sl < entry:
        return "Buy"
    if sl > entry:
        return "Sell"
    return ""


def extract_entry_price(text: str, label_entry: str = "") -> float:
    """Цена входа: метка / текущая цена / меняем цену открытия на …"""
    if label_entry:
        raw = _extract_after_label(text, label_entry)
        if raw and not re.search(r"не\s+меняем|без\s+изменен", raw, re.I):
            v = first_price_in(raw)
            if v > 0:
                return v
    patterns = [
        rf"текущая\s+цена\s*:?\s*{_PRICE_RE}",
        rf"цен[ауы]\s+открытия\s*(?:на\s*)?:?\s*{_PRICE_RE}",
        rf"меняем\s+цен[уа]\s+открытия\s*(?:на\s*)?:?\s*{_PRICE_RE}",
        rf"меняем\s+цен[уа]\s*(?:на\s*)?:?\s*{_PRICE_RE}",
        rf"открытия\s*(?:на\s*)?:?\s*{_PRICE_RE}",
        rf"(?:entry|вход)\s*:?\s*{_PRICE_RE}",
    ]
    for pat in patterns:
        m = re.search(pat, text or "", re.IGNORECASE)
        if m:
            return parse_price(m.group(1))
    return 0.0


def extract_sl_price(text: str, label_sl: str = "") -> float:
    """SL. «стоп лосс не меняем» → 0 (наследование), не цеплять чужую цену с строки."""
    # явные формулировки смены стопа
    for pat in (
        rf"стоп[\s\-]*лосс\s+(?:обратно\s+)?(?:выставляем\s+)?(?:переносим\s+)?(?:на|в|=|:)\s*{_PRICE_RE}",
        rf"выставляем\s+стоп[\s\-]*лосс\s+(?:обратно\s+)?(?:на\s*)?{_PRICE_RE}",
        rf"стоп[\s\-]*лосс\s*:?\s*{_PRICE_RE}",
        rf"\bsl\s*(?:to|=|:)?\s*{_PRICE_RE}",
    ):
        m = re.search(pat, text or "", re.IGNORECASE)
        if m:
            # если сразу после метки «не меняем» — не число стопа
            start = m.start()
            window = (text or "")[start : start + 40].lower()
            if re.search(r"стоп[\s\-]*лосс\s+не\s+меня", window):
                continue
            return parse_price(m.group(1))

    if label_sl:
        raw = _extract_after_label(text, label_sl)
        if raw:
            if re.search(r"не\s+меняем|без\s+изменен", raw, re.I):
                return 0.0
            # число должно идти сразу / через «на», не через другой label
            m = re.match(
                rf"^(?:на\s*)?:?\s*{_PRICE_RE}",
                raw.strip(),
                re.IGNORECASE,
            )
            if m:
                return parse_price(m.group(1))
            # классическая строка «Стоп лосс: 1.23»
            v = first_price_in(raw.split(".")[0] if False else raw)
            # если до первого числа есть «цена открытия/тейк» — не наш SL
            before = raw.lower()
            idx = re.search(r"\d", before)
            if idx:
                head = before[: idx.start()]
                if re.search(r"цен[аыу].*открыт|тейк|take|entry", head):
                    return 0.0
                # число в пределах короткого хвоста после метки
                if idx.start() <= 12:
                    return first_price_in(raw)
    return 0.0


def extract_tp_price(
    text: str, label_tp: str = "", tp_open_words: list[str] | None = None
) -> tuple[float, bool]:
    """Возвращает (tp, tp_open)."""
    if label_tp:
        raw = _extract_after_label(text, label_tp)
        if raw:
            if tp_open_words and _contains_any(raw.lower(), tp_open_words):
                return 0.0, True
            if re.search(r"не\s+меняем|без\s+изменен", raw, re.I):
                return 0.0, True
            m = re.match(rf"^(?:на\s*)?:?\s*{_PRICE_RE}", raw.strip(), re.I)
            if m:
                return parse_price(m.group(1)), False
            v = first_price_in(raw)
            if v > 0 and not re.search(r"цен[аыу].*открыт|стоп", raw.lower()[:20]):
                return v, False
    m = re.search(
        rf"тейк[\s\-]*профит\s*:?\s*(открыт\w*|{_PRICE_RE})",
        text or "",
        re.IGNORECASE,
    )
    if m:
        token = m.group(1)
        if token and "открыт" in token.lower():
            return 0.0, True
        v = parse_price(token)
        if v > 0:
            return v, False
    if re.search(
        r"тейк[\s\-]*профит\s+оставляем\s+открыт|открытый\s+тейк|tp\s*open|"
        r"цели\s+не\s+меня|тейк\s+не\s+меня",
        text or "",
        re.I,
    ):
        return 0.0, True
    return 0.0, True


def detect_side_label(
    side_blob: str, full_text: str, buy_words: list[str], sell_words: list[str]
) -> str:
    """Сторона: сначала явная метка ордера, затем действие входа, затем геометрия-текст."""
    text_l = (full_text or "").lower()
    blob = (side_blob or "").strip()
    blob_l = blob.lower()

    # 1) формальная / явная метка стороны (важнее комментария «тчк в лонг»)
    head = blob_l[:160] if blob_l else ""
    if re.search(r"на\s+продаж|лимит\w*\s+на\s+продаж|продаж[аиуё]\s+по\s+рынк|\bshort\b|\bsell\b", head):
        return "Sell"
    if re.search(r"на\s+покуп|лимит\w*\s+на\s+покуп|покупк[аиу]\s+по\s+рынк|\blong\b|\bbuy\b", head):
        return "Buy"
    if blob:
        if _contains_any(blob_l, sell_words) and not re.search(r"\(.*(?:лонг|покуп)", blob_l):
            # «на продажу» уже поймали; чисто sell-слова в метке
            if re.search(r"продаж|шорт|sell|short", blob_l.split("(")[0]):
                return "Sell"
        if _contains_any(blob_l, buy_words) and not re.search(r"\(.*(?:шорт|продаж)", blob_l):
            if re.search(r"покуп|лонг|buy|long", blob_l.split("(")[0]):
                return "Buy"

    # 2) разворот в тексте
    if re.search(r"закрыва\w+\s+(?:продаж|шорт).{0,60}(?:лонг|покуп)", text_l):
        return "Buy"
    if re.search(r"закрыва\w+\s+(?:покуп|лонг).{0,60}(?:шорт|продаж)", text_l):
        return "Sell"

    # 3) последнее явное действие входа (короткое окно — без длинного комментария)
    head_t = text_l[:280]
    acts = list(
        re.finditer(
            r"(?:заходим|открываем|открываемся|перезаходим)\s*"
            r"(?:по\s+рынку\s*[,.]?\s*)?(?:в\s+)?"
            r"(покупк\w*|продаж\w*|лонг\w*|шорт\w*)|"
            r"(?:покупка|продажа)\s+по\s+рынк|"
            r"в\s+(покупку|продажу)|"
            r"\b(long|short|buy|sell)\b",
            head_t,
        )
    )
    if acts:
        g = acts[-1].group(0)
        if re.search(r"покуп|лонг|\blong\b|\bbuy\b", g):
            return "Buy"
        if re.search(r"продаж|шорт|\bshort\b|\bsell\b", g):
            return "Sell"

    if re.search(r"покупк|заходим\s+в\s+лонг|в\s+лонг|\bbuy\b|\blong\b", head_t) and not re.search(
        r"продаж|шорт|\bsell\b|\bshort\b", head_t
    ):
        return "Buy"
    if re.search(r"продаж|шорт|\bsell\b|\bshort\b", head_t) and not re.search(
        r"покупк|лонг|\bbuy\b|\blong\b", head_t
    ):
        return "Sell"
    if _contains_any(head_t, buy_words) and not _contains_any(head_t, sell_words):
        return "Buy"
    if _contains_any(head_t, sell_words) and not _contains_any(head_t, buy_words):
        return "Sell"
    return ""


def synth_tp(side: str, entry: float, sl: float, rr: float) -> float:
    risk = abs(entry - sl)
    if risk <= 0 or rr <= 0:
        return 0.0
    if side.lower().startswith("b"):
        return entry + rr * risk
    return entry - rr * risk


def build_signal(
    message_id: int,
    date_utc: str,
    channel: str,
    text: str,
    rules: ParseRules,
    source: str,
) -> ParsedSignal | None:
    if not text or not text.strip():
        return None

    text = normalize_signal_text(text)
    text_l = text.lower()
    username = channel.lstrip("@")
    link = f"https://t.me/{username}/{message_id}"

    has_symbol_tag = bool(SYMBOL_HASH_RE.search(text)) if rules.symbol_from_hashtag else False
    manage_hit = bool(
        rules.manage_enabled
        and rules.manage_trigger_words
        and _contains_any(text_l, rules.manage_trigger_words)
    )

    # обязательные слова: при manage / наличии #symbol пропускаем жёсткий фильтр
    if rules.must_contain:
        if not all(w.lower() in text_l for w in rules.must_contain):
            if not (manage_hit or (rules.manage_enabled and has_symbol_tag)):
                return None

    # пропуск: при выключенном сопровождении — как раньше
    if rules.skip_if_contains and _contains_any(text_l, rules.skip_if_contains):
        if not rules.manage_enabled:
            if rules.fmt == "labels" and rules.label_side:
                if rules.label_side.lower() not in text_l:
                    return None
            else:
                return None
        # manage ON: не отбрасываем — разберём как сопровождение / сигнал

    symbol = ""
    if rules.symbol_from_hashtag:
        sm = SYMBOL_HASH_RE.search(text)
        if sm:
            symbol = sm.group(1).upper()

    side = ""
    order_type = "market"
    entry = sl = tp = 0.0
    tp_open = False
    timeframe = ""
    note = ""
    inherit = levels_inherited(text, rules.inherit_words)

    if rules.fmt == "compact":
        # #eurcad EUR/CAD H1 Buy 1.6038 SL 1.5975 TP 1.6020
        buy = re.escape(rules.compact_side_buy)
        sell = re.escape(rules.compact_side_sell)
        slw = re.escape(rules.compact_sl_word)
        tpw = re.escape(rules.compact_tp_word)
        pat = re.compile(
            rf"""
            \#(?P<tag>[\w]+)
            \s+
            (?:(?P<pair>[A-Za-z0-9]+/[A-Za-z0-9]+)\s+)?
            (?:(?P<tf>M\d+|H\d+|D\d+|W\d+)\s+)?
            (?P<side>{buy}|{sell})
            \s+
            (?P<entry>\d+(?:[.,]\d+)?)
            \s*
            {slw}\s*(?P<sl>\d+(?:[.,]\d+)?)
            \s*
            {tpw}\s*(?P<tp>\d+(?:[.,]\d+)?)
            """,
            re.IGNORECASE | re.VERBOSE,
        )
        m = pat.search(text)
        if not m:
            return None
        symbol = (m.group("pair") or m.group("tag")).replace("/", "").upper()
        side = "Buy" if m.group("side").lower() == rules.compact_side_buy.lower() else "Sell"
        # normalize Buy/Sell from actual match
        if re.search(buy, m.group("side"), re.I):
            side = "Buy"
        else:
            side = "Sell"
        entry = parse_price(m.group("entry"))
        sl = parse_price(m.group("sl"))
        tp = parse_price(m.group("tp"))
        timeframe = (m.group("tf") or "").upper()
    else:
        # labels format
        side_blob = _extract_after_label(text, rules.label_side) if rules.label_side else ""
        blob_l = (side_blob or text).lower()
        if _contains_any(blob_l, rules.limit_words) or (
            not side_blob and ("лимит" in text_l or "limit" in text_l)
        ):
            order_type = "limit"
        side = detect_side_label(side_blob, text, rules.buy_words, rules.sell_words)

        entry = extract_entry_price(text, rules.label_entry)
        sl = extract_sl_price(text, rules.label_sl)
        tp, tp_open_flag = extract_tp_price(text, rules.label_tp, rules.tp_open_words)
        if tp_open_flag or tp <= 0:
            tp_open = True
            tp = 0.0
        else:
            tp_open = False

        # «Тейк профит выставляем на 1.31920» — смена TP в сопровождении
        if tp <= 0:
            m_tp_set = re.search(
                rf"(?:тейк[\s\-]*профит|take[\s\-]*profit|\btp\b)\s*"
                rf"(?:выставляем|ставим|меняем|переносим|двигаем|set|to|at)?\s*"
                rf"(?:на\s*)?:?\s*{_PRICE_RE}",
                text,
                re.IGNORECASE,
            )
            if m_tp_set:
                tp = parse_price(m_tp_set.group(1))
                if tp > 0:
                    tp_open = False

        # короткий формат / добор, если ещё пусто
        short = re.search(
            rf"(?:текущая\s+цена|цен[ауы]\s+открытия|открытия)\s*(?:на\s*)?:?\s*{_PRICE_RE}"
            rf".{{0,120}}?"
            rf"(?:стоп[\s\-]*лосс)\s*(?:на\s*)?:?\s*{_PRICE_RE}",
            text,
            re.IGNORECASE | re.DOTALL,
        )
        if short and not inherit:
            if entry <= 0:
                entry = parse_price(short.group(1))
            if sl <= 0:
                sl = parse_price(short.group(2))

        # «по рынку» / «лимитный» в коротких постах (удаление лимита → market)
        if re.search(r"лимит\w*\s+ордер\s+удаля|ордер\s+удаля|лимитку\s+удаля", text_l):
            order_type = "market"
        elif "лимит" in text_l or "limit" in text_l:
            if "рынк" not in text_l:
                order_type = "limit"
        elif "рынк" in text_l or "market" in text_l:
            order_type = "market"

    # сторона из геометрии уровней, если явно не сказали
    if not side:
        side = infer_side_from_levels(entry, sl)

    # ложный SL == entry из «не меняем» + цена открытия в той же строке
    if inherit and entry > 0 and sl > 0 and abs(sl - entry) < 1e-9:
        sl = 0.0

    # формальный сигнал с полными уровнями — не «наследуем» из комментария
    formal_like = bool(rules.label_side and rules.label_side.lower() in text_l)
    if formal_like and entry > 0 and sl > 0:
        inherit = False

    def _fail(msg: str, **kw) -> ParsedSignal:
        return ParsedSignal(
            message_id=message_id,
            date_utc=date_utc,
            link=link,
            raw_text=text,
            symbol=kw.get("symbol", symbol),
            side=kw.get("side", side),
            order_type=order_type,
            entry=kw.get("entry", entry),
            sl=kw.get("sl", sl),
            tp=kw.get("tp", tp),
            tp_open=tp_open,
            timeframe=timeframe,
            parse_ok=False,
            parse_note=msg,
            source=source,
            inherit_levels=inherit,
        )

    # вход по рынку с наследованием SL/TP из цепочки
    if (
        symbol
        and (side or inherit)
        and (entry > 0 or (inherit and ("рынк" in text_l or "market" in text_l)))
        and sl <= 0
        and inherit
    ):
        note = "inherit_levels"
        # side может подтянуть manage из цепочки
        return ParsedSignal(
            message_id=message_id,
            date_utc=date_utc,
            link=link,
            raw_text=text,
            symbol=symbol,
            side=side,
            order_type=order_type,
            entry=entry,
            sl=0.0,
            tp=tp,
            tp_open=True if tp <= 0 else tp_open,
            timeframe=timeframe,
            parse_ok=True,
            parse_note=note,
            source=source,
            inherit_levels=True,
        )

    if not symbol or not side or entry <= 0 or sl <= 0:
        return _fail("Не хватает symbol/side/entry/SL")

    if side == "Buy" and not (sl < entry):
        return _fail("BUY: SL должен быть ниже entry")
    if side == "Sell" and not (sl > entry):
        return _fail("SELL: SL должен быть выше entry")

    if tp_open or tp <= 0:
        tp_open = True
        tp = synth_tp(side, entry, sl, rules.open_tp_rr)
        note = f"tp_synth_rr={rules.open_tp_rr}"
        if tp <= 0:
            return _fail("Не удалось вычислить TP", tp=0)

    return ParsedSignal(
        message_id=message_id,
        date_utc=date_utc,
        link=link,
        raw_text=text,
        symbol=symbol,
        side=side,
        order_type=order_type,
        entry=entry,
        sl=sl,
        tp=tp,
        tp_open=tp_open,
        timeframe=timeframe,
        parse_ok=True,
        parse_note=note,
        source=source,
        inherit_levels=inherit,
    )


def fetch_web_page(url: str) -> str:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36"
            )
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read().decode("utf-8", "replace")


def parse_web_messages(page_html: str) -> list[dict]:
    matches = list(MESSAGE_BLOCK_RE.finditer(page_html))
    out: list[dict] = []
    for i, m in enumerate(matches):
        start = m.start()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(page_html)
        block = page_html[start:end]
        dt_m = DATETIME_RE.search(block)
        if not dt_m:
            continue
        text_m = TEXT_RE.search(block)
        text = html_to_text(text_m.group("body")) if text_m else ""
        out.append(
            {
                "channel": m.group("channel"),
                "message_id": int(m.group("id")),
                "date": datetime.fromisoformat(dt_m.group("dt")),
                "text": text,
            }
        )
    return out


def fetch_signals_web(channel: str, days_back: int, rules: ParseRules) -> list[ParsedSignal]:
    username = channel.lstrip("@")
    since = datetime.now(timezone.utc) - timedelta(days=days_back)
    before: int | None = None
    seen: set[int] = set()
    signals: list[ParsedSignal] = []
    pages = 0
    reached_old = False
    max_pages = max(80, days_back * 2)

    print(f"Канал: @{username} | web | {days_back} дн.")
    while pages < max_pages and not reached_old:
        url = (
            f"https://t.me/s/{username}"
            if before is None
            else f"https://t.me/s/{username}?before={before}"
        )
        pages += 1
        print(f"  страница {pages}")
        messages = parse_web_messages(fetch_web_page(url))
        if not messages:
            break
        oldest = min(m["message_id"] for m in messages)
        new_n = 0
        for msg in messages:
            mid = msg["message_id"]
            if mid in seen:
                continue
            seen.add(mid)
            new_n += 1
            msg_dt = msg["date"]
            if msg_dt.tzinfo is None:
                msg_dt = msg_dt.replace(tzinfo=timezone.utc)
            if msg_dt < since:
                reached_old = True
                continue
            parsed = build_signal(
                mid,
                msg_dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
                channel,
                msg["text"],
                rules,
                "web",
            )
            if parsed:
                signals.append(parsed)
        if new_n == 0:
            break
        if before is not None and oldest >= before:
            break
        before = oldest
        time.sleep(0.3)

    signals.sort(key=lambda s: (s.date_utc, s.message_id), reverse=True)
    return signals


async def fetch_signals_telethon(
    channel: str,
    days_back: int,
    rules: ParseRules,
    api_id: int,
    api_hash: str,
    phone: str | None,
) -> list[ParsedSignal]:
    from telethon import TelegramClient
    from telethon.tl.types import Message

    client = TelegramClient(str(cfg.SESSION_PATH), api_id, api_hash)
    await client.connect()
    if not await client.is_user_authorized():
        await client.disconnect()
        raise RuntimeError(
            "Нет авторизации Telegram. Войдите через панель (Прислать код → ввести код)."
        )
    since = datetime.now(timezone.utc) - timedelta(days=days_back)
    print(f"Канал: {channel} | telethon | {days_back} дн.")
    signals: list[ParsedSignal] = []
    scanned = 0
    async for msg in client.iter_messages(channel):
        if not isinstance(msg, Message) or msg.date is None:
            continue
        msg_dt = msg.date if msg.date.tzinfo else msg.date.replace(tzinfo=timezone.utc)
        if msg_dt < since:
            break
        scanned += 1
        if scanned % 400 == 0:
            print(f"  ... {scanned} сообщений, сигналов {len(signals)}")
        parsed = build_signal(
            msg.id,
            msg_dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
            channel,
            (msg.message or "").strip(),
            rules,
            "telethon",
        )
        if parsed:
            signals.append(parsed)
    await client.disconnect()
    signals.sort(key=lambda s: (s.date_utc, s.message_id), reverse=True)
    print(f"Просмотрено: {scanned}, кандидатов: {len(signals)}")
    return signals


def save_outputs(signals: list[ParsedSignal], channel: str, days_back: int, mode: str) -> dict:
    from .manage import build_chains, chains_to_dicts, events_to_dicts, manage_from_settings

    out = cfg.OUTPUT_DIR
    out.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    cp = cfg.load_settings()
    mr = manage_from_settings(cp)
    rules = rules_from_settings(cp)
    events, chains, annotated = build_chains(
        signals, mr, label_side=rules.label_side, fmt=rules.fmt
    )
    signals = annotated

    ok = [s for s in signals if s.parse_ok]
    fail = [s for s in signals if not s.parse_ok]
    manage_n = sum(1 for s in ok if getattr(s, "role", "") == "manage")
    signal_n = sum(1 for s in ok if getattr(s, "role", "") == "signal")

    payload = {
        "channel": channel,
        "mode": mode,
        "days_back": days_back,
        "manage_enabled": mr.enabled,
        "parsed_ok": len(ok),
        "parse_failed": len(fail),
        "signals_open": signal_n,
        "manage_updates": manage_n,
        "chains": len(chains),
        "timeline_events": len(events),
        "signals": [asdict(s) for s in signals],
        "chains_detail": chains_to_dicts(chains),
        "timeline": events_to_dicts(events),
    }
    (out / "signals_latest.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (out / "timeline_latest.json").write_text(
        json.dumps(
            {"manage_enabled": mr.enabled, "events": events_to_dicts(events), "chains": chains_to_dicts(chains)},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    fields = list(asdict(ok[0]).keys()) if ok else list(asdict(signals[0]).keys()) if signals else []
    for name in ("signals_latest.csv", f"signals_{stamp}.csv"):
        with (out / name).open("w", encoding="utf-8-sig", newline="") as f:
            w = csv.DictWriter(f, fieldnames=fields)
            w.writeheader()
            for s in signals:
                w.writerow(asdict(s))

    # timeline CSV for backtest / live
    tl_fields = [
        "chain_id", "root_id", "msg_id", "time_utc", "symbol", "action",
        "side", "order_type", "entry", "sl", "tp", "tp_open", "parent_id", "note", "link",
    ]
    for name in ("timeline_latest.csv", f"timeline_{stamp}.csv"):
        with (out / name).open("w", encoding="utf-8-sig", newline="") as f:
            w = csv.DictWriter(f, fieldnames=tl_fields)
            w.writeheader()
            for e in events:
                w.writerow(asdict(e))

    lines = [
        f"# Сигналы {channel} ({days_back} дн., {mode})",
        "",
        f"- Успешно: **{len(ok)}** | Ошибки: **{len(fail)}**",
        f"- Новые входы: **{signal_n}** | Сопровождение: **{manage_n}** | Цепочек: **{len(chains)}**",
        f"- Сопровождение сделок: **{'вкл' if mr.enabled else 'выкл'}**",
        "",
        "| Дата | Роль | Цепочка | Символ | Сторона | Ордер | Entry | SL | TP | Action |",
        "|---|---|---:|---|---|---|---:|---:|---:|---|",
    ]
    for s in ok:
        lines.append(
            f"| {s.date_utc} | {s.role} | {s.chain_id} | {s.symbol} | {s.side} | "
            f"{s.order_type} | {s.entry} | {s.sl} | {s.tp} | {s.action} |"
        )
    (out / "signals_latest.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

    # MT5 CSV: открытия из таймлайна (action=open) — для совместимости
    mt5_dir = out / "mt5"
    mt5_dir.mkdir(exist_ok=True)
    header = [
        "time", "symbol", "side", "entry", "sl", "tp", "tf", "msg_id",
        "order_type", "tp_open", "chain_id", "root_id", "action", "parent_id",
    ]
    rows = []
    for e in events:
        try:
            dt = datetime.strptime(e.time_utc, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue
        rows.append(
            [
                dt.strftime("%Y.%m.%d %H:%M:%S"),
                e.symbol,
                (e.side or "").upper(),
                f"{e.entry:.8f}".rstrip("0").rstrip("."),
                f"{e.sl:.8f}".rstrip("0").rstrip("."),
                f"{e.tp:.8f}".rstrip("0").rstrip("."),
                "H1",
                str(e.msg_id),
                e.order_type,
                "1" if e.tp_open else "0",
                str(e.chain_id),
                str(e.root_id),
                e.action,
                str(e.parent_id),
            ]
        )

    def write_csv(path: Path, data):
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8", newline="") as f:
            w = csv.writer(f, delimiter=";")
            w.writerow(header)
            w.writerows(data)

    write_csv(mt5_dir / "signals.csv", rows)
    write_csv(mt5_dir / "timeline.csv", rows)
    by: dict[str, list] = {}
    for r in rows:
        if r[12] == "open":  # action
            by.setdefault(r[1], []).append(r)
    for sym, rr in by.items():
        write_csv(mt5_dir / "by_symbol" / f"{sym}.csv", rr)

    common = cfg.mt5_common_files()
    if common:
        dest = common / "SignalKit"
        write_csv(dest / "signals.csv", rows)
        write_csv(dest / "timeline.csv", rows)
        for sym, rr in by.items():
            write_csv(dest / "by_symbol" / f"{sym}.csv", rr)

    return {
        "ok": len(ok),
        "fail": len(fail),
        "total": len(signals),
        "chains": len(chains),
        "manage": manage_n,
        "opens": signal_n,
        "out": str(out),
    }


def run_parse(mode: str | None = None) -> dict:
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

    cp = cfg.load_settings()
    channel = cfg.get(cp, "telegram", "channel")
    if not channel:
        raise ValueError("Укажите channel в НАСТРОЙКИ.ini → [telegram]")

    days = cfg.get_int(cp, "period", "days_back", 30)
    fetch_mode = (mode or cfg.get(cp, "telegram", "fetch_mode", "telethon")).lower()
    rules = rules_from_settings(cp)

    if fetch_mode == "web":
        signals = fetch_signals_web(channel, days, rules)
    else:
        api_id = cfg.get(cp, "telegram", "api_id")
        api_hash = cfg.get(cp, "telegram", "api_hash")
        phone = cfg.get(cp, "telegram", "phone") or None
        if not api_id or not api_hash:
            raise ValueError(
                "Для полной истории нужны api_id и api_hash в [telegram]. "
                "Или поставьте fetch_mode=web для короткой публичной ленты."
            )
        from .tg_auth import ensure_authorized

        ensure_authorized()
        signals = asyncio.run(
            fetch_signals_telethon(
                channel if channel.startswith("@") else f"@{channel}",
                days,
                rules,
                int(api_id),
                api_hash,
                phone,
            )
        )

    stats = save_outputs(signals, channel, days, fetch_mode)
    cfg.export_live_rules(cp)
    print(f"Готово: OK={stats['ok']} FAIL={stats['fail']} → {stats['out']}")
    return stats
