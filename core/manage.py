# -*- coding: utf-8 -*-
"""
Сопровождение сделок: цепочки сообщений (сигнал → корректировки).

Универсально: правила и ключевые слова берутся из [manage] в НАСТРОЙКИ.ini.
При manage.enabled=no поведение как раньше (management-посты отсекаются skip_if_contains).
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from typing import Any

from . import config as cfg


def _split_list(s: str) -> list[str]:
    if not s:
        return []
    return [p.strip() for p in re.split(r"[|;\n]+", s) if p.strip()]


def _has(text_l: str, words: list[str]) -> bool:
    return any(w.lower() in text_l for w in words if w)


@dataclass
class ManageRules:
    enabled: bool = False
    link_max_hours: int = 336
    link_max_id_gap: int = 200
    informal_as_manage: bool = True
    words_cancel_pending: list[str] = field(default_factory=list)
    words_reverse: list[str] = field(default_factory=list)
    words_breakeven: list[str] = field(default_factory=list)
    words_close: list[str] = field(default_factory=list)
    words_modify_sl: list[str] = field(default_factory=list)
    words_modify_tp: list[str] = field(default_factory=list)
    words_clear_expiry: list[str] = field(default_factory=list)
    words_add: list[str] = field(default_factory=list)
    words_to_market: list[str] = field(default_factory=list)
    words_keep_pending: list[str] = field(default_factory=list)
    words_inherit_levels: list[str] = field(default_factory=list)


def manage_from_settings(cp) -> ManageRules:
    en = cfg.get(cp, "manage", "enabled", "no").lower() in ("yes", "1", "true", "да", "on")
    return ManageRules(
        enabled=en,
        link_max_hours=cfg.get_int(cp, "manage", "link_max_hours", 720),
        link_max_id_gap=cfg.get_int(cp, "manage", "link_max_id_gap", 200),
        informal_as_manage=cfg.get(cp, "manage", "informal_as_manage", "yes").lower()
        in ("yes", "1", "true", "да"),
        words_cancel_pending=_split_list(
            cfg.get(
                cp,
                "manage",
                "words_cancel_pending",
                "лимитный ордер удаляем|ордер удаляем|лимитку удаляем|удаляем лимит|"
                "лимитный ордер снимаем|не ушел в работу|не ушёл в работу|"
                "сценарий уже не актуален|сценарий не актуален|уже не актуален|"
                "ордер снимаем|снимаем ордер|отменяем ордер|order not filled|"
                "idea no longer valid|setup invalid",
            )
        ),
        words_reverse=_split_list(
            cfg.get(
                cp,
                "manage",
                "words_reverse",
                "перезаходим|разворот|в противополож|закрываем продажу и|закрываем покупку и",
            )
        ),
        words_breakeven=_split_list(
            cfg.get(
                cp,
                "manage",
                "words_breakeven",
                "безубыток|безубытк|стоп в бе|переносим стоп|стоп лосс переносим|"
                "на точку входа|точку входа|точки входа|в безубыток|breakeven|break even",
            )
        ),
        words_close=_split_list(
            cfg.get(
                cp,
                "manage",
                "words_close",
                "фиксируем прибыль|фиксируем сделку|фиксируем обе|фиксируем полностью|"
                "фиксируем всю|фиксируем результат|закрываем полностью|закрываем сделку|"
                "закрываем обе|выходим из сделки|закрыли сделку|закрыли полностью|"
                "close trade|close position|close fully|book profit|take profit fully",
            )
        ),
        words_modify_sl=_split_list(
            cfg.get(
                cp,
                "manage",
                "words_modify_sl",
                "стоп лосс перемещаем|стоп лосс меняем|стоп лосс по открытой|меняем стоп|"
                "выставляем стоп|стоп лосс обратно|стоп лосс на|выставляем стоп лосс",
            )
        ),
        words_modify_tp=_split_list(
            cfg.get(
                cp,
                "manage",
                "words_modify_tp",
                "тейк профит выставляем|тейк профит ставим|тейк профит меняем|"
                "тейк профит переносим|тейк профит на|выставляем тейк|меняем тейк|"
                "take profit to|tp to|set tp",
            )
        ),
        words_clear_expiry=_split_list(
            cfg.get(
                cp,
                "manage",
                "words_clear_expiry",
                "убираем дату истечения|ордер до отмены|без даты истечения|"
                "good till cancel|gtc|remove expiry|no expiry|until cancelled|until canceled",
            )
        ),
        words_add=_split_list(
            cfg.get(
                cp,
                "manage",
                "words_add",
                "еще один ордер|ещё один ордер|усредн|добираем|открываем еще",
            )
        ),
        words_to_market=_split_list(
            cfg.get(
                cp,
                "manage",
                "words_to_market",
                "по рынку|по рыночн|рыночной цене|открываемся по рынку|заходим по рынку|"
                "открываем по рынку|открываем шорт по рынку|открываем лонг по рынку",
            )
        ),
        words_keep_pending=_split_list(
            cfg.get(
                cp,
                "manage",
                "words_keep_pending",
                "лимитку не удаляем|лимит не удаляем|ордер не удаляем",
            )
        ),
        words_inherit_levels=_split_list(
            cfg.get(
                cp,
                "manage",
                "words_inherit_levels",
                "не меняем|без изменений|параметры без|остальные параметры|"
                "keep sl|sl unchanged|levels unchanged|same stop",
            )
        ),
    )


def is_formal_signal(text: str, label_side: str, fmt: str) -> bool:
    tl = (text or "").lower()
    if fmt == "labels" and label_side and label_side.lower() in tl:
        return True
    return False


def detect_action(text: str, mr: ManageRules, formal: bool) -> str:
    """Определяет тип действия для сообщения."""
    tl = (text or "").lower()
    # Формальный новый сигнал (есть «Тип сделки» и т.п.) — всегда open,
    # даже если в комментарии упомянут безубыток как план.
    if formal:
        return "open"
    # полное закрытие раньше BE: «фиксируем прибыль», «закрываем сделку»
    if _is_close_text(tl, mr):
        return "close"
    # снятие неактуального лимита
    if _is_cancel_pending_text(tl, mr):
        # «удаляем лимит и открываем по рынку» — replace, не чистый cancel
        if _has(tl, mr.words_to_market) or "цена открытия" in tl or _has(tl, mr.words_inherit_levels):
            if _has(tl, mr.words_cancel_pending) or re.search(
                r"лимит\w*\s+ордер\s+удаля|ордер\s+удаля|лимитку\s+удаля", tl
            ):
                return "replace_market"
        return "cancel_pending"
    # безубыток / перенос стопа на вход
    if _is_breakeven_text(tl, mr):
        return "breakeven"
    if _has(tl, mr.words_reverse) or re.search(
        r"закрыва\w+\s+(?:продаж|покуп|шорт|лонг).{0,40}(?:заходим|открываем)", tl
    ):
        return "reverse"
    # смена TP: «тейк профит выставляем на …»
    if _is_modify_tp_text(tl, mr):
        return "modify_tp"
    # убрать дату истечения / GTC
    if _is_clear_expiry_text(tl, mr):
        return "clear_expiry"
    # смена уровней лимита: «меняем цену открытия на …»
    if _is_modify_levels_text(tl):
        return "modify_levels"
    if _has(tl, mr.words_cancel_pending) and (
        _has(tl, mr.words_to_market) or "цена открытия" in tl or _has(tl, mr.words_inherit_levels)
    ):
        return "replace_market"
    if _has(tl, mr.words_cancel_pending):
        return "cancel_pending"
    if _has(tl, mr.words_add):
        return "add"
    if _has(tl, mr.words_modify_sl):
        return "modify_sl"
    # рынок + наследование параметров / явный вход по рынку в сопровождении
    if _has(tl, mr.words_to_market) and (
        _has(tl, mr.words_inherit_levels) or "цена открытия" in tl or "открыва" in tl
    ):
        return "replace_market"
    if _has(tl, mr.words_to_market) or "цена открытия" in tl or "стоп лосс" in tl:
        return "replace_or_open"
    return "manage"


_CLOSE_RE = re.compile(
    r"(?:"
    r"фиксир\w*\s+(?:прибыл|сделк|обе|полност|всю|результат|позиц)"
    r"|закрыв\w*\s+(?:полност|сделк|обе|позиц|всю)"
    r"|выходим\s+из\s+сделк"
    r"|закрыли\s+(?:сделк|полност|позиц|всю)"
    r"|close(?:\s+the)?\s+(?:trade|position|fully)"
    r"|closing\s+(?:the\s+)?(?:trade|position)"
    r"|book\s+profits?"
    r"|take\s+profits?\s+fully"
    r"|fully\s+closed?"
    r")",
    re.IGNORECASE,
)

_CANCEL_RE = re.compile(
    r"(?:"
    r"не\s+уш[её]л\s+в\s+работ"
    r"|сценарий\s+(?:уже\s+)?не\s+актуал"
    r"|(?:уже\s+)?не\s+актуален"
    r"|ордер\s+снима"
    r"|снимаем\s+(?:лимит|ордер)"
    r"|отменяем\s+(?:лимит|ордер)"
    r"|order\s+(?:not\s+filled|cancelled|canceled)"
    r"|idea\s+(?:is\s+)?no\s+longer\s+valid"
    r"|setup\s+(?:is\s+)?invalid"
    r")",
    re.IGNORECASE,
)

_MODIFY_TP_RE = re.compile(
    r"(?:"
    r"тейк[\s\-]*профит\s*(?:выставляем|ставим|меняем|переносим|двигаем)?\s*(?:на\s*)?:?"
    r"|выставляем\s+тейк"
    r"|меняем\s+тейк"
    r"|take[\s\-]*profit\s*(?:to|at|=)?"
    r"|\btp\s*(?:to|at|=|set)?"
    r")\s*"
    r"(\d{1,6}(?:[.,]\d{1,6})?)",
    re.IGNORECASE,
)

_CLEAR_EXPIRY_RE = re.compile(
    r"(?:"
    r"убираем\s+дат[уы]\s+истечен"
    r"|ордер\s+до\s+отмен"
    r"|без\s+дат[ыи]\s+истечен"
    r"|good\s+till\s+cancel|\bgtc\b"
    r"|remove\s+expir"
    r"|no\s+expiry"
    r"|until\s+cancell?ed"
    r")",
    re.IGNORECASE,
)


def _is_close_text(tl: str, mr: ManageRules) -> bool:
    """Полное закрытие позиции (не «пока держим»)."""
    if _has(tl, mr.words_close):
        return True
    if _CLOSE_RE.search(tl or ""):
        # «закрываем продажу и открываем покупку» — это reverse, не close
        if re.search(
            r"закрыва\w+\s+(?:продаж|покуп|шорт|лонг).{0,40}(?:заходим|открываем|перезаход)",
            tl or "",
        ):
            return False
        return True
    return False


def _is_cancel_pending_text(tl: str, mr: ManageRules) -> bool:
    if _has(tl, mr.words_cancel_pending):
        return True
    return bool(_CANCEL_RE.search(tl or ""))


def _is_modify_tp_text(tl: str, mr: ManageRules) -> bool:
    if _has(tl, mr.words_modify_tp):
        return True
    return bool(_MODIFY_TP_RE.search(tl or ""))


def extract_modify_tp_price(text: str) -> float:
    """Цена нового TP из текста сопровождения."""
    from .parser import parse_price

    m = _MODIFY_TP_RE.search(text or "")
    if m:
        return parse_price(m.group(1))
    return 0.0


def _is_clear_expiry_text(tl: str, mr: ManageRules) -> bool:
    if _has(tl, mr.words_clear_expiry):
        return True
    return bool(_CLEAR_EXPIRY_RE.search(tl or ""))


def _fill_from_chain(s: Any, st: dict) -> None:
    """Подставляет side/SL/TP из активной цепочки, если в посте «не меняем»."""
    if not getattr(s, "side", None):
        s.side = st["side"]
    old_entry = float(st.get("init_entry") or st.get("entry") or 0)
    old_sl = float(st.get("init_sl") or st.get("sl") or 0)
    if float(getattr(s, "sl", 0) or 0) <= 0:
        # текущий стоп цепочки (может быть уже BE) — как база
        s.sl = float(st.get("sl") or 0) or old_sl
    if float(getattr(s, "tp", 0) or 0) <= 0:
        s.tp = float(st["tp"])
        s.tp_open = st.get("tp_open", True)
    if not getattr(s, "order_type", None):
        s.order_type = "market"

    entry = float(getattr(s, "entry", 0) or 0)
    sl = float(getattr(s, "sl", 0) or 0)
    side = (getattr(s, "side", None) or st.get("side") or "").strip()
    # если новый вход ломает геометрию со старым SL — сохраняем дистанцию риска
    if entry > 0 and side:
        ok = sl > 0 and (
            (side.lower().startswith("b") and sl < entry)
            or (side.lower().startswith("s") and sl > entry)
        )
        if not ok:
            risk = abs(old_entry - old_sl) if old_entry > 0 and old_sl > 0 else 0.0
            if risk <= 1e-12:
                # стоп уже был в BE — возьмём небольшой риск ~0.2% цены
                risk = abs(entry) * 0.002
            if risk > 0:
                s.sl = entry - risk if side.lower().startswith("b") else entry + risk
                sl = float(s.sl)

    if getattr(s, "tp_open", False) or float(s.tp or 0) <= 0:
        if entry > 0 and float(s.sl or 0) > 0 and side:
            try:
                from .parser import synth_tp

                s.tp = synth_tp(side, entry, float(s.sl), 2.0)
                s.tp_open = True
            except Exception:
                s.tp = float(st["tp"])


def _side_flipped(s: Any, st: dict) -> bool:
    a = (getattr(s, "side", None) or "").lower()
    b = (st.get("side") or "").lower()
    return bool(a and b and a != b)


def _set_init_levels(st: dict, entry: float, sl: float, *, reset: bool = False) -> None:
    """Якорь риска для inherit после BE / смены входа."""
    if reset or "init_entry" not in st:
        st["init_entry"] = float(entry or 0)
        st["init_sl"] = float(sl or 0)
    elif float(st.get("init_entry") or 0) <= 0:
        st["init_entry"] = float(entry or 0)
        st["init_sl"] = float(sl or 0)


def _is_modify_levels_text(tl: str) -> bool:
    return bool(
        re.search(
            r"меняем\s+цен[уа]|сменили\s+цен|цену\s+открытия\s+на|"
            r"новую\s+цен[уа]\s+открытия|переставляем\s+(?:лимит|ордер|цен)",
            tl or "",
        )
    )


def _is_breakeven_text(tl: str, mr: ManageRules) -> bool:
    # не путать с «цена у точки входа, закрываем…»
    if re.search(r"закрыва\w+\s+(сделк|позици|полностью)|выходим\s+из\s+сделк", tl or ""):
        return False
    if _has(tl, mr.words_breakeven):
        return True
    # типичные формулировки каналов
    patterns = (
        "точку входа",
        "точки входа",
        "точка входа",
        "на точку входа",
        "стоп в безубыт",
        "в безубыт",
        "безубыт",
        "переносим стоп",
        "переносим стоп-лосс",
        "переносим стоп лосс",
        "стоп лосс переносим",
        "стоп-лосс переносим",
        "стоп на вход",
        "сл в бе",
        "sl to be",
        "sl to entry",
        "move sl to entry",
        "breakeven",
        "break even",
    )
    return any(p in tl for p in patterns)


def _parse_dt(s: str) -> datetime:
    return datetime.strptime(s, "%Y-%m-%d %H:%M:%S")


@dataclass
class TimelineEvent:
    chain_id: int
    root_id: int
    msg_id: int
    time_utc: str
    symbol: str
    action: str
    side: str
    order_type: str
    entry: float
    sl: float
    tp: float
    tp_open: bool
    parent_id: int = 0
    note: str = ""
    link: str = ""


@dataclass
class ChainInfo:
    chain_id: int
    root_id: int
    symbol: str
    msg_ids: list[int]
    events: list[str]
    closed: bool = False


def build_chains(
    signals: list[Any],
    mr: ManageRules,
    label_side: str = "Тип сделки",
    fmt: str = "labels",
) -> tuple[list[TimelineEvent], list[ChainInfo], list[Any]]:
    """
    Строит цепочки и таймлайн событий.
    signals — список ParsedSignal (или совместимых объектов), любой порядок.
    Возвращает: events, chains, annotated_signals (с role/action/chain_id).
    """
    if not mr.enabled:
        events: list[TimelineEvent] = []
        annotated = []
        for s in sorted(signals, key=lambda x: (_parse_dt(x.date_utc), x.message_id)):
            if not getattr(s, "parse_ok", False):
                annotated.append(s)
                continue
            s.role = "signal"
            s.action = "open"
            s.chain_id = s.message_id
            s.parent_id = 0
            s.formal = is_formal_signal(s.raw_text, label_side, fmt)
            annotated.append(s)
            events.append(
                TimelineEvent(
                    chain_id=s.message_id,
                    root_id=s.message_id,
                    msg_id=s.message_id,
                    time_utc=s.date_utc,
                    symbol=s.symbol,
                    action="open",
                    side=s.side,
                    order_type=s.order_type,
                    entry=s.entry,
                    sl=s.sl,
                    tp=s.tp,
                    tp_open=s.tp_open,
                    note=s.parse_note,
                    link=s.link,
                )
            )
        chains = [
            ChainInfo(e.chain_id, e.root_id, e.symbol, [e.msg_id], [e.action])
            for e in events
        ]
        return events, chains, annotated

    ordered = sorted(signals, key=lambda x: (_parse_dt(x.date_utc), x.message_id))
    # symbol -> active chain state
    active: dict[str, dict] = {}
    events = []
    chains_map: dict[int, ChainInfo] = {}
    annotated = []

    def expire_stale(sym: str, now: datetime, mid: int) -> None:
        st = active.get(sym)
        if not st:
            return
        age_h = (now - st["last_dt"]).total_seconds() / 3600.0
        id_gap = mid - st["last_id"]
        if age_h > mr.link_max_hours or id_gap > mr.link_max_id_gap:
            chains_map[st["chain_id"]].closed = True
            active.pop(sym, None)

    for s in ordered:
        formal = is_formal_signal(s.raw_text, label_side, fmt)
        s.formal = formal
        now = _parse_dt(s.date_utc)

        if not getattr(s, "parse_ok", False):
            # manage-only без полного entry/sl: всё равно попробуем привязать
            action = detect_action(s.raw_text or "", mr, formal)
            tl0 = (s.raw_text or "").lower()
            inherit = bool(getattr(s, "inherit_levels", False)) or _has(
                tl0, mr.words_inherit_levels
            )
            soft_ok = action in (
                "close",
                "breakeven",
                "cancel_pending",
                "modify_sl",
                "modify_tp",
                "modify_levels",
                "clear_expiry",
                "replace_market",
                "replace_or_open",
                "reverse",
            )
            if inherit and action in ("manage", "open", "replace_or_open"):
                action = "replace_market"
                soft_ok = True
            if action == "modify_tp" and float(getattr(s, "tp", 0) or 0) <= 0:
                s.tp = extract_modify_tp_price(s.raw_text or "")
            # «меняем цену» часто без стороны — достаточно entry (+sl)
            if action == "modify_levels" and float(getattr(s, "entry", 0) or 0) <= 0:
                soft_ok = False
            if action == "modify_tp" and float(getattr(s, "tp", 0) or 0) <= 0:
                soft_ok = False
            if s.symbol and soft_ok:
                expire_stale(s.symbol, now, s.message_id)
                st = active.get(s.symbol)
                if st and action in ("replace_market", "replace_or_open", "reverse"):
                    _fill_from_chain(s, st)
                    if float(s.sl or 0) <= 0:
                        s.role = "noise"
                        annotated.append(s)
                        continue
                    # если сторона сменилась — reverse, иначе replace
                    if action == "reverse" or _side_flipped(s, st):
                        action = "reverse"
                    else:
                        action = "replace_market"
                    s.parse_ok = True
                    s.role = "manage"
                    s.action = action
                    s.chain_id = st["chain_id"]
                    s.parent_id = st["root_id"]
                    s.parse_note = (s.parse_note or "") + f"|chain={action}"
                    cid = st["chain_id"]
                    # дальше — тот же код, что у parse_ok ветки: эмулируем через метку
                    # (вставляем события прямо здесь)
                    if action == "reverse":
                        events.append(
                            TimelineEvent(
                                chain_id=cid,
                                root_id=st["root_id"],
                                msg_id=s.message_id,
                                time_utc=s.date_utc,
                                symbol=s.symbol,
                                action="close",
                                side=st["side"],
                                order_type=st["order_type"],
                                entry=st["entry"],
                                sl=st["sl"],
                                tp=st["tp"],
                                tp_open=st["tp_open"],
                                parent_id=st["root_id"],
                                note="reverse_close",
                                link=s.link,
                            )
                        )
                        events.append(
                            TimelineEvent(
                                chain_id=cid,
                                root_id=st["root_id"],
                                msg_id=s.message_id,
                                time_utc=s.date_utc,
                                symbol=s.symbol,
                                action="open",
                                side=s.side,
                                order_type=s.order_type or "market",
                                entry=s.entry if float(s.entry or 0) > 0 else st["entry"],
                                sl=s.sl,
                                tp=s.tp,
                                tp_open=s.tp_open,
                                parent_id=st["root_id"],
                                note="reverse_open",
                                link=s.link,
                            )
                        )
                        st.update(
                            {
                                "side": s.side,
                                "order_type": s.order_type or "market",
                                "entry": s.entry if float(s.entry or 0) > 0 else st["entry"],
                                "sl": s.sl,
                                "tp": s.tp,
                                "tp_open": s.tp_open,
                                "pending": False,
                                "last_dt": now,
                                "last_id": s.message_id,
                            }
                        )
                        _set_init_levels(
                            st,
                            float(s.entry or st["entry"] or 0),
                            float(s.sl or 0),
                            reset=True,
                        )
                        chains_map[cid].msg_ids.append(s.message_id)
                        chains_map[cid].events.extend(["close", "open"])
                    else:
                        keep = _has(tl0, mr.words_keep_pending)
                        if st.get("pending") and not keep:
                            events.append(
                                TimelineEvent(
                                    chain_id=cid,
                                    root_id=st["root_id"],
                                    msg_id=s.message_id,
                                    time_utc=s.date_utc,
                                    symbol=s.symbol,
                                    action="cancel_pending",
                                    side=st["side"],
                                    order_type="limit",
                                    entry=st["entry"],
                                    sl=st["sl"],
                                    tp=st["tp"],
                                    tp_open=st["tp_open"],
                                    parent_id=st["root_id"],
                                    note="cancel_then_market",
                                    link=s.link,
                                )
                            )
                            chains_map[cid].events.append("cancel_pending")
                        entry_v = float(s.entry or 0)
                        # рынок без цены в тексте — entry=0, бэктест возьмёт open бара
                        events.append(
                            TimelineEvent(
                                chain_id=cid,
                                root_id=st["root_id"],
                                msg_id=s.message_id,
                                time_utc=s.date_utc,
                                symbol=s.symbol,
                                action="open",
                                side=s.side or st["side"],
                                order_type="market",
                                entry=entry_v,
                                sl=s.sl,
                                tp=s.tp if float(s.tp or 0) > 0 else st["tp"],
                                tp_open=s.tp_open,
                                parent_id=st["root_id"],
                                note="replace_market",
                                link=s.link,
                            )
                        )
                        st.update(
                            {
                                "side": s.side or st["side"],
                                "order_type": "market",
                                "entry": entry_v if entry_v > 0 else st["entry"],
                                "sl": s.sl,
                                "tp": s.tp if float(s.tp or 0) > 0 else st["tp"],
                                "tp_open": s.tp_open,
                                "pending": False,
                                "last_dt": now,
                                "last_id": s.message_id,
                            }
                        )
                        _set_init_levels(
                            st,
                            float(entry_v if entry_v > 0 else st["entry"] or 0),
                            float(s.sl or 0),
                            reset=True,
                        )
                        chains_map[cid].msg_ids.append(s.message_id)
                        chains_map[cid].events.append("open")
                    annotated.append(s)
                    continue

                if st:
                    if action == "modify_levels":
                        new_entry = float(s.entry) if float(s.entry or 0) > 0 else float(st["entry"])
                        new_sl = float(s.sl) if float(s.sl or 0) > 0 else float(st["sl"])
                        new_tp = float(s.tp) if float(s.tp or 0) > 0 else float(st["tp"])
                        if st.get("tp_open") and new_entry > 0 and new_sl > 0:
                            try:
                                from .parser import synth_tp

                                new_tp = synth_tp(st["side"], new_entry, new_sl, 2.0) or new_tp
                            except Exception:
                                pass
                        s.role = "manage"
                        s.action = "modify_levels"
                        s.chain_id = st["chain_id"]
                        s.parent_id = st["root_id"]
                        s.parse_ok = True
                        s.side = s.side or st["side"]
                        s.entry = new_entry
                        s.sl = new_sl
                        s.tp = new_tp
                        s.order_type = s.order_type or st["order_type"]
                        s.parse_note = (s.parse_note or "") + "|chain=modify_levels"
                        events.append(
                            TimelineEvent(
                                chain_id=st["chain_id"],
                                root_id=st["root_id"],
                                msg_id=s.message_id,
                                time_utc=s.date_utc,
                                symbol=s.symbol,
                                action="modify_levels",
                                side=st["side"],
                                order_type=st["order_type"],
                                entry=new_entry,
                                sl=new_sl,
                                tp=new_tp,
                                tp_open=st["tp_open"],
                                parent_id=st["root_id"],
                                note="modify_levels",
                                link=s.link,
                            )
                        )
                        st.update(
                            {
                                "entry": new_entry,
                                "sl": new_sl,
                                "tp": new_tp,
                                "last_dt": now,
                                "last_id": s.message_id,
                            }
                        )
                        chains_map[st["chain_id"]].msg_ids.append(s.message_id)
                        chains_map[st["chain_id"]].events.append("modify_levels")
                        annotated.append(s)
                        continue

                    if action == "modify_tp":
                        new_tp = float(s.tp) if float(s.tp or 0) > 0 else extract_modify_tp_price(
                            s.raw_text or ""
                        )
                        if new_tp <= 0:
                            s.role = "noise"
                            annotated.append(s)
                            continue
                        s.role = "manage"
                        s.action = "modify_tp"
                        s.chain_id = st["chain_id"]
                        s.parent_id = st["root_id"]
                        s.parse_ok = True
                        s.side = s.side or st["side"]
                        s.entry = s.entry or st["entry"]
                        s.sl = s.sl or st["sl"]
                        s.tp = new_tp
                        s.tp_open = False
                        s.order_type = s.order_type or st["order_type"]
                        s.parse_note = (s.parse_note or "") + "|chain=modify_tp"
                        events.append(
                            TimelineEvent(
                                chain_id=st["chain_id"],
                                root_id=st["root_id"],
                                msg_id=s.message_id,
                                time_utc=s.date_utc,
                                symbol=s.symbol,
                                action="modify_levels",
                                side=st["side"],
                                order_type=st["order_type"],
                                entry=st["entry"],
                                sl=st["sl"],
                                tp=new_tp,
                                tp_open=False,
                                parent_id=st["root_id"],
                                note="modify_tp",
                                link=s.link,
                            )
                        )
                        st["tp"] = new_tp
                        st["tp_open"] = False
                        st["last_dt"] = now
                        st["last_id"] = s.message_id
                        chains_map[st["chain_id"]].msg_ids.append(s.message_id)
                        chains_map[st["chain_id"]].events.append("modify_tp")
                        annotated.append(s)
                        continue

                    if action == "clear_expiry":
                        s.role = "manage"
                        s.action = "clear_expiry"
                        s.chain_id = st["chain_id"]
                        s.parent_id = st["root_id"]
                        s.parse_ok = True
                        s.side = s.side or st["side"]
                        s.entry = s.entry or st["entry"]
                        s.sl = s.sl or st["sl"]
                        s.tp = s.tp or st["tp"]
                        s.order_type = s.order_type or st["order_type"]
                        s.parse_note = (s.parse_note or "") + "|chain=clear_expiry"
                        events.append(
                            TimelineEvent(
                                chain_id=st["chain_id"],
                                root_id=st["root_id"],
                                msg_id=s.message_id,
                                time_utc=s.date_utc,
                                symbol=s.symbol,
                                action="clear_expiry",
                                side=st["side"],
                                order_type=st["order_type"],
                                entry=st["entry"],
                                sl=st["sl"],
                                tp=st["tp"],
                                tp_open=st["tp_open"],
                                parent_id=st["root_id"],
                                note="gtc",
                                link=s.link,
                            )
                        )
                        st["gtc"] = True
                        st.pop("expire_at", None)
                        st["last_dt"] = now
                        st["last_id"] = s.message_id
                        chains_map[st["chain_id"]].msg_ids.append(s.message_id)
                        chains_map[st["chain_id"]].events.append("clear_expiry")
                        annotated.append(s)
                        continue

                    if action == "breakeven":
                        new_sl = float(st["entry"])
                        ev_action = "modify_sl"
                        note = "breakeven→entry"
                    elif action == "modify_sl" and s.sl > 0:
                        new_sl = float(s.sl)
                        ev_action = "modify_sl"
                        note = "modify_sl"
                    elif action == "modify_sl":
                        new_sl = float(st["entry"])
                        ev_action = "modify_sl"
                        note = "modify_sl→entry"
                    else:
                        new_sl = float(st["sl"])
                        ev_action = action
                        note = action

                    s.role = "manage"
                    s.action = "breakeven" if "breakeven" in note or action == "breakeven" else action
                    s.chain_id = st["chain_id"]
                    s.parent_id = st["root_id"]
                    s.parse_ok = True
                    s.side = s.side or st["side"]
                    s.entry = s.entry or st["entry"]
                    s.sl = new_sl if ev_action == "modify_sl" else (s.sl or st["sl"])
                    s.tp = s.tp or st["tp"]
                    s.order_type = s.order_type or st["order_type"]
                    s.parse_note = (s.parse_note or "") + f"|chain={s.action}"
                    ev = TimelineEvent(
                        chain_id=st["chain_id"],
                        root_id=st["root_id"],
                        msg_id=s.message_id,
                        time_utc=s.date_utc,
                        symbol=s.symbol,
                        action=ev_action,
                        side=st["side"],
                        order_type=st["order_type"],
                        entry=st["entry"],
                        sl=new_sl if ev_action == "modify_sl" else st["sl"],
                        tp=st["tp"],
                        tp_open=st["tp_open"],
                        parent_id=st["root_id"],
                        note=note,
                        link=s.link,
                    )
                    if ev_action == "modify_sl":
                        st["sl"] = new_sl
                    events.append(ev)
                    chains_map[st["chain_id"]].msg_ids.append(s.message_id)
                    chains_map[st["chain_id"]].events.append(s.action or ev_action)
                    st["last_dt"] = now
                    st["last_id"] = s.message_id
                    if action == "close":
                        chains_map[st["chain_id"]].closed = True
                        active.pop(s.symbol, None)
                    elif action == "cancel_pending":
                        chains_map[st["chain_id"]].closed = True
                        active.pop(s.symbol, None)
                    annotated.append(s)
                    continue
            s.role = "noise"
            s.action = ""
            s.chain_id = 0
            s.parent_id = 0
            annotated.append(s)
            continue

        expire_stale(s.symbol, now, s.message_id)
        st = active.get(s.symbol)
        action = detect_action(s.raw_text or "", mr, formal)

        # inherit / пустой SL → только сопровождение (не для формальных полных сигналов)
        if (not formal) and (
            getattr(s, "inherit_levels", False) or float(getattr(s, "sl", 0) or 0) <= 0
        ):
            if not st:
                s.parse_ok = False
                s.role = "noise"
                s.action = ""
                s.chain_id = 0
                s.parse_note = (s.parse_note or "") + "|no_chain_for_inherit"
                annotated.append(s)
                continue
            _fill_from_chain(s, st)
            if action in ("open", "manage", "replace_or_open", "replace_market") or _side_flipped(
                s, st
            ):
                if _side_flipped(s, st):
                    action = "reverse"
                elif action in ("open", "manage", "replace_or_open", "replace_market"):
                    action = "replace_market"

        # Формальный новый сигнал → всегда новая цепочка
        start_new = formal and action == "open"
        # Неформальный без активной цепочки → новая
        if not st:
            start_new = True
        elif not formal and mr.informal_as_manage:
            # есть активная цепочка — это сопровождение
            start_new = False
            if action == "open":
                action = "replace_or_open"
        elif formal:
            start_new = True

        # inherit не может открыть новую сделку с нуля
        if start_new and (not formal) and (
            getattr(s, "inherit_levels", False) or float(s.sl or 0) <= 0
        ):
            s.parse_ok = False
            s.role = "noise"
            s.action = ""
            s.chain_id = 0
            s.parse_note = (s.parse_note or "") + "|inherit_without_chain"
            annotated.append(s)
            continue

        if start_new:
            if st:
                chains_map[st["chain_id"]].closed = True
            cid = s.message_id
            s.role = "signal"
            s.action = "open"
            s.chain_id = cid
            s.parent_id = 0
            ev = TimelineEvent(
                chain_id=cid,
                root_id=cid,
                msg_id=s.message_id,
                time_utc=s.date_utc,
                symbol=s.symbol,
                action="open",
                side=s.side,
                order_type=s.order_type,
                entry=s.entry,
                sl=s.sl,
                tp=s.tp,
                tp_open=s.tp_open,
                note=s.parse_note,
                link=s.link,
            )
            events.append(ev)
            chains_map[cid] = ChainInfo(cid, cid, s.symbol, [s.message_id], ["open"])
            active[s.symbol] = {
                "chain_id": cid,
                "root_id": cid,
                "side": s.side,
                "order_type": s.order_type,
                "entry": s.entry,
                "sl": s.sl,
                "tp": s.tp,
                "tp_open": s.tp_open,
                "init_entry": float(s.entry or 0),
                "init_sl": float(s.sl or 0),
                "last_dt": now,
                "last_id": s.message_id,
                "pending": s.order_type == "limit",
            }
            annotated.append(s)
            continue

        # --- сопровождение существующей цепочки ---
        assert st is not None
        cid = st["chain_id"]
        s.role = "manage"
        s.chain_id = cid
        s.parent_id = st["root_id"]

        if action == "replace_or_open":
            # если есть cancel pending или to_market — replace; иначе если side сменилась — reverse
            tl = (s.raw_text or "").lower()
            if _is_modify_levels_text(tl):
                action = "modify_levels"
            elif _has(tl, mr.words_reverse) or (
                s.side and st["side"] and s.side.lower() != st["side"].lower()
            ):
                action = "reverse"
            elif _has(tl, mr.words_cancel_pending) or _has(tl, mr.words_to_market):
                action = "replace_market"
            elif abs(s.entry - st["entry"]) < 1e-9 and s.sl > 0:
                action = "modify_sl"
            else:
                action = "replace_market"

        s.action = action

        if action == "modify_levels":
            new_entry = float(s.entry) if float(s.entry or 0) > 0 else float(st["entry"])
            new_sl = float(s.sl) if float(s.sl or 0) > 0 else float(st["sl"])
            new_tp = float(s.tp) if float(s.tp or 0) > 0 else float(st["tp"])
            if st.get("tp_open") and new_entry > 0 and new_sl > 0:
                try:
                    from .parser import synth_tp

                    new_tp = synth_tp(st["side"] or s.side, new_entry, new_sl, 2.0) or new_tp
                except Exception:
                    pass
            events.append(
                TimelineEvent(
                    chain_id=cid,
                    root_id=st["root_id"],
                    msg_id=s.message_id,
                    time_utc=s.date_utc,
                    symbol=s.symbol,
                    action="modify_levels",
                    side=st["side"],
                    order_type=st["order_type"],
                    entry=new_entry,
                    sl=new_sl,
                    tp=new_tp,
                    tp_open=st["tp_open"],
                    parent_id=st["root_id"],
                    note="modify_levels",
                    link=s.link,
                )
            )
            st.update(
                {
                    "entry": new_entry,
                    "sl": new_sl,
                    "tp": new_tp,
                    "last_dt": now,
                    "last_id": s.message_id,
                }
            )
            chains_map[cid].msg_ids.append(s.message_id)
            chains_map[cid].events.append("modify_levels")

        elif action == "reverse":
            # закрыть текущую + открыть новую сторону в той же цепочке
            events.append(
                TimelineEvent(
                    chain_id=cid,
                    root_id=st["root_id"],
                    msg_id=s.message_id,
                    time_utc=s.date_utc,
                    symbol=s.symbol,
                    action="close",
                    side=st["side"],
                    order_type=st["order_type"],
                    entry=st["entry"],
                    sl=st["sl"],
                    tp=st["tp"],
                    tp_open=st["tp_open"],
                    parent_id=st["root_id"],
                    note="reverse_close",
                    link=s.link,
                )
            )
            events.append(
                TimelineEvent(
                    chain_id=cid,
                    root_id=st["root_id"],
                    msg_id=s.message_id,
                    time_utc=s.date_utc,
                    symbol=s.symbol,
                    action="open",
                    side=s.side,
                    order_type=s.order_type or "market",
                    entry=s.entry,
                    sl=s.sl,
                    tp=s.tp,
                    tp_open=s.tp_open,
                    parent_id=st["root_id"],
                    note="reverse_open",
                    link=s.link,
                )
            )
            st.update(
                {
                    "side": s.side,
                    "order_type": s.order_type or "market",
                    "entry": s.entry,
                    "sl": s.sl,
                    "tp": s.tp,
                    "tp_open": s.tp_open,
                    "pending": (s.order_type or "market") == "limit",
                    "last_dt": now,
                    "last_id": s.message_id,
                }
            )
            _set_init_levels(st, float(s.entry or 0), float(s.sl or 0), reset=True)
            chains_map[cid].msg_ids.append(s.message_id)
            chains_map[cid].events.extend(["close", "open"])

        elif action == "replace_market":
            keep = _has((s.raw_text or "").lower(), mr.words_keep_pending)
            if st.get("pending") and not keep:
                events.append(
                    TimelineEvent(
                        chain_id=cid,
                        root_id=st["root_id"],
                        msg_id=s.message_id,
                        time_utc=s.date_utc,
                        symbol=s.symbol,
                        action="cancel_pending",
                        side=st["side"],
                        order_type="limit",
                        entry=st["entry"],
                        sl=st["sl"],
                        tp=st["tp"],
                        tp_open=st["tp_open"],
                        parent_id=st["root_id"],
                        note="cancel_then_market",
                        link=s.link,
                    )
                )
                chains_map[cid].events.append("cancel_pending")
            events.append(
                TimelineEvent(
                    chain_id=cid,
                    root_id=st["root_id"],
                    msg_id=s.message_id,
                    time_utc=s.date_utc,
                    symbol=s.symbol,
                    action="open",
                    side=s.side or st["side"],
                    order_type="market",
                    entry=s.entry,
                    sl=s.sl if s.sl > 0 else st["sl"],
                    tp=s.tp if s.tp > 0 else st["tp"],
                    tp_open=s.tp_open,
                    parent_id=st["root_id"],
                    note="replace_market",
                    link=s.link,
                )
            )
            st.update(
                {
                    "side": s.side or st["side"],
                    "order_type": "market",
                    "entry": s.entry,
                    "sl": s.sl if s.sl > 0 else st["sl"],
                    "tp": s.tp if s.tp > 0 else st["tp"],
                    "tp_open": s.tp_open,
                    "pending": False,
                    "last_dt": now,
                    "last_id": s.message_id,
                }
            )
            _set_init_levels(
                st,
                float(s.entry or st["entry"] or 0),
                float((s.sl if s.sl > 0 else st["sl"]) or 0),
                reset=True,
            )
            chains_map[cid].msg_ids.append(s.message_id)
            chains_map[cid].events.append("open")

        elif action == "cancel_pending":
            events.append(
                TimelineEvent(
                    chain_id=cid,
                    root_id=st["root_id"],
                    msg_id=s.message_id,
                    time_utc=s.date_utc,
                    symbol=s.symbol,
                    action="cancel_pending",
                    side=st["side"],
                    order_type="limit",
                    entry=st["entry"],
                    sl=st["sl"],
                    tp=st["tp"],
                    tp_open=st["tp_open"],
                    parent_id=st["root_id"],
                    link=s.link,
                )
            )
            st["pending"] = False
            st["last_dt"] = now
            st["last_id"] = s.message_id
            chains_map[cid].msg_ids.append(s.message_id)
            chains_map[cid].events.append("cancel_pending")
            chains_map[cid].closed = True
            active.pop(s.symbol, None)

        elif action == "modify_tp":
            new_tp = float(s.tp) if float(s.tp or 0) > 0 else extract_modify_tp_price(s.raw_text or "")
            if new_tp <= 0:
                annotated.append(s)
                continue
            s.action = "modify_tp"
            s.tp = new_tp
            s.tp_open = False
            events.append(
                TimelineEvent(
                    chain_id=cid,
                    root_id=st["root_id"],
                    msg_id=s.message_id,
                    time_utc=s.date_utc,
                    symbol=s.symbol,
                    action="modify_levels",
                    side=st["side"],
                    order_type=st["order_type"],
                    entry=st["entry"],
                    sl=st["sl"],
                    tp=new_tp,
                    tp_open=False,
                    parent_id=st["root_id"],
                    note="modify_tp",
                    link=s.link,
                )
            )
            st["tp"] = new_tp
            st["tp_open"] = False
            st["last_dt"] = now
            st["last_id"] = s.message_id
            chains_map[cid].msg_ids.append(s.message_id)
            chains_map[cid].events.append("modify_tp")

        elif action == "clear_expiry":
            s.action = "clear_expiry"
            events.append(
                TimelineEvent(
                    chain_id=cid,
                    root_id=st["root_id"],
                    msg_id=s.message_id,
                    time_utc=s.date_utc,
                    symbol=s.symbol,
                    action="clear_expiry",
                    side=st["side"],
                    order_type=st["order_type"],
                    entry=st["entry"],
                    sl=st["sl"],
                    tp=st["tp"],
                    tp_open=st["tp_open"],
                    parent_id=st["root_id"],
                    note="gtc",
                    link=s.link,
                )
            )
            st["gtc"] = True
            st.pop("expire_at", None)
            st["last_dt"] = now
            st["last_id"] = s.message_id
            chains_map[cid].msg_ids.append(s.message_id)
            chains_map[cid].events.append("clear_expiry")

        elif action in ("modify_sl", "breakeven"):
            if action == "breakeven":
                new_sl = st["entry"]
                s.action = "breakeven"
                note = "breakeven→entry"
            elif s.sl > 0:
                new_sl = s.sl
                note = "modify_sl"
            elif s.entry > 0 and abs(s.entry - st["entry"]) > 1e-9:
                # новый уровень стопа иногда единственное число
                new_sl = s.entry
                note = "modify_sl"
            else:
                new_sl = st["entry"]
                s.action = "breakeven"
                note = "breakeven→entry"
            events.append(
                TimelineEvent(
                    chain_id=cid,
                    root_id=st["root_id"],
                    msg_id=s.message_id,
                    time_utc=s.date_utc,
                    symbol=s.symbol,
                    action="modify_sl",
                    side=st["side"],
                    order_type=st["order_type"],
                    entry=st["entry"],
                    sl=new_sl,
                    tp=st["tp"],
                    tp_open=st["tp_open"],
                    parent_id=st["root_id"],
                    note=note,
                    link=s.link,
                )
            )
            st["sl"] = new_sl
            # если в том же посте есть новый лимит/вход — открыть add
            if (
                action != "breakeven"
                and s.entry > 0
                and abs(s.entry - st["entry"]) > 1e-9
                and s.side
                and note == "modify_sl"
            ):
                events.append(
                    TimelineEvent(
                        chain_id=cid,
                        root_id=st["root_id"],
                        msg_id=s.message_id,
                        time_utc=s.date_utc,
                        symbol=s.symbol,
                        action="open",
                        side=s.side,
                        order_type=s.order_type or "limit",
                        entry=s.entry,
                        sl=s.sl if s.sl > 0 else new_sl,
                        tp=s.tp,
                        tp_open=s.tp_open,
                        parent_id=st["root_id"],
                        note="add_after_modify",
                        link=s.link,
                    )
                )
                chains_map[cid].events.append("open")
                st.update(
                    {
                        "side": s.side,
                        "order_type": s.order_type or "limit",
                        "entry": s.entry,
                        "sl": s.sl if s.sl > 0 else new_sl,
                        "tp": s.tp,
                        "pending": (s.order_type or "limit") == "limit",
                    }
                )
            st["last_dt"] = now
            st["last_id"] = s.message_id
            chains_map[cid].msg_ids.append(s.message_id)
            chains_map[cid].events.append(s.action or "modify_sl")

        elif action == "add":
            events.append(
                TimelineEvent(
                    chain_id=cid,
                    root_id=st["root_id"],
                    msg_id=s.message_id,
                    time_utc=s.date_utc,
                    symbol=s.symbol,
                    action="open",
                    side=s.side or st["side"],
                    order_type=s.order_type or "market",
                    entry=s.entry,
                    sl=s.sl if s.sl > 0 else st["sl"],
                    tp=s.tp if s.tp > 0 else st["tp"],
                    tp_open=s.tp_open,
                    parent_id=st["root_id"],
                    note="add",
                    link=s.link,
                )
            )
            if s.sl > 0:
                events.append(
                    TimelineEvent(
                        chain_id=cid,
                        root_id=st["root_id"],
                        msg_id=s.message_id,
                        time_utc=s.date_utc,
                        symbol=s.symbol,
                        action="modify_sl",
                        side=st["side"],
                        order_type=st["order_type"],
                        entry=st["entry"],
                        sl=s.sl,
                        tp=st["tp"],
                        tp_open=st["tp_open"],
                        parent_id=st["root_id"],
                        note="sl_on_add",
                        link=s.link,
                    )
                )
                st["sl"] = s.sl
                chains_map[cid].events.append("modify_sl")
            st["last_dt"] = now
            st["last_id"] = s.message_id
            chains_map[cid].msg_ids.append(s.message_id)
            chains_map[cid].events.append("open")

        elif action == "close":
            events.append(
                TimelineEvent(
                    chain_id=cid,
                    root_id=st["root_id"],
                    msg_id=s.message_id,
                    time_utc=s.date_utc,
                    symbol=s.symbol,
                    action="close",
                    side=st["side"],
                    order_type=st["order_type"],
                    entry=st["entry"],
                    sl=st["sl"],
                    tp=st["tp"],
                    tp_open=st["tp_open"],
                    parent_id=st["root_id"],
                    link=s.link,
                )
            )
            chains_map[cid].msg_ids.append(s.message_id)
            chains_map[cid].events.append("close")
            chains_map[cid].closed = True
            active.pop(s.symbol, None)

        else:
            # generic manage with levels → replace_market
            events.append(
                TimelineEvent(
                    chain_id=cid,
                    root_id=st["root_id"],
                    msg_id=s.message_id,
                    time_utc=s.date_utc,
                    symbol=s.symbol,
                    action="open",
                    side=s.side or st["side"],
                    order_type=s.order_type or "market",
                    entry=s.entry,
                    sl=s.sl if s.sl > 0 else st["sl"],
                    tp=s.tp if s.tp > 0 else st["tp"],
                    tp_open=s.tp_open,
                    parent_id=st["root_id"],
                    note=action or "manage",
                    link=s.link,
                )
            )
            st.update(
                {
                    "side": s.side or st["side"],
                    "order_type": s.order_type or "market",
                    "entry": s.entry,
                    "sl": s.sl if s.sl > 0 else st["sl"],
                    "tp": s.tp if s.tp > 0 else st["tp"],
                    "last_dt": now,
                    "last_id": s.message_id,
                    "pending": (s.order_type or "market") == "limit",
                }
            )
            chains_map[cid].msg_ids.append(s.message_id)
            chains_map[cid].events.append("open")

        annotated.append(s)

    return events, list(chains_map.values()), annotated


def events_to_dicts(events: list[TimelineEvent]) -> list[dict]:
    return [asdict(e) for e in events]


def chains_to_dicts(chains: list[ChainInfo]) -> list[dict]:
    return [asdict(c) for c in chains]
