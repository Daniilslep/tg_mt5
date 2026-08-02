# -*- coding: utf-8 -*-
"""Вход в Telegram для telethon — код в панели ИЛИ готовая .session."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from . import config as cfg

_STATE: dict[str, Any] = {
    "phone": None,
    "phone_code_hash": None,
    "need_password": False,
}


def _run(coro):
    return asyncio.run(coro)


async def _client(api_id: int, api_hash: str, session_path: str | Path | None = None):
    from telethon import TelegramClient

    path = session_path or cfg.SESSION_PATH
    return TelegramClient(str(path), api_id, api_hash)


def _candidate_sessions() -> list[Path]:
    """Только локальная сессия этого проекта (без чужих папок)."""
    root = cfg.ROOT
    names = [
        root / "tg_session.session",
    ]
    out: list[Path] = []
    for p in names:
        if p.exists() and p.stat().st_size > 0:
            out.append(p)
    return out


def import_existing_session() -> dict:
    """Проверить, есть ли уже рабочая локальная сессия tg_session."""
    cp = cfg.load_settings()
    api_id = cfg.get(cp, "telegram", "api_id")
    api_hash = cfg.get(cp, "telegram", "api_hash")
    if not api_id or not api_hash:
        return {"ok": False, "error": "Нет api_id / api_hash"}

    dest_file = Path(str(cfg.SESSION_PATH) + ".session")

    async def _try_one(sess_file: Path) -> dict | None:
        base = str(sess_file)[:-8] if sess_file.name.endswith(".session") else str(sess_file)
        client = await _client(int(api_id), api_hash, base)
        await client.connect()
        ok = await client.is_user_authorized()
        me = None
        phone = None
        if ok:
            u = await client.get_me()
            me = getattr(u, "username", None) or str(u.id)
            phone = getattr(u, "phone", None)
        await client.disconnect()
        if not ok:
            return None
        return {"ok": True, "imported": False, "user": me, "phone": phone, "source": str(sess_file)}

    for sess in _candidate_sessions():
        try:
            res = _run(_try_one(sess))
        except Exception:
            continue
        if res and res.get("ok"):
            if res.get("phone"):
                if not cp.has_section("telegram"):
                    cp.add_section("telegram")
                phone = res["phone"]
                if not str(phone).startswith("+"):
                    phone = "+" + str(phone)
                cp.set("telegram", "phone", phone)
                cfg.save_settings(cp)
            res["message"] = (
                f"Вход готов ({res.get('user')}). "
                "Текущая сессия уже рабочая. Можно нажимать «Скачать сигналы»."
            )
            return res

    return {
        "ok": False,
        "error": (
            "Готовой сессии не найдено. Пройдите вход по коду из Telegram "
            "(кнопка «Отправить код» в панели) или положите свой файл "
            "tg_session.session в корень проекта. "
            "Код обычно приходит в чат «Telegram» в приложении. "
            "Либо используйте fetch_mode=web для короткой публичной ленты."
        ),
    }


def status() -> dict:
    cp = cfg.load_settings()
    api_id = cfg.get(cp, "telegram", "api_id")
    api_hash = cfg.get(cp, "telegram", "api_hash")
    phone = cfg.get(cp, "telegram", "phone")
    if not api_id or not api_hash:
        return {
            "authorized": False,
            "need": "api",
            "message": "Сначала сохраните api_id и api_hash в настройках.",
            "phone": phone,
        }

    async def _check():
        client = await _client(int(api_id), api_hash)
        await client.connect()
        ok = await client.is_user_authorized()
        me = None
        if ok:
            u = await client.get_me()
            me = getattr(u, "username", None) or getattr(u, "first_name", None) or str(u.id)
        await client.disconnect()
        return ok, me

    try:
        ok, me = _run(_check())
    except Exception as e:
        return {"authorized": False, "need": "error", "message": str(e), "phone": phone}

    if ok:
        return {
            "authorized": True,
            "need": None,
            "message": f"Telegram вход выполнен ({me}). Можно скачивать сигналы.",
            "phone": phone,
            "user": me,
        }

    # не авторизованы
    local_sess = (cfg.ROOT / "tg_session.session").exists()
    return {
        "authorized": False,
        "need": "login",
        "message": (
            "Нужен вход в Telegram: укажите phone, сохраните, нажмите «Отправить код». "
            "Код приходит в чат «Telegram» в приложении (не SMS). "
            "Либо переключите fetch_mode=web для короткой публичной ленты."
        ),
        "phone": phone,
        "waiting_code": bool(_STATE.get("phone_code_hash")),
        "need_password": bool(_STATE.get("need_password")),
        "can_import": local_sess,
    }


def send_code(phone: str | None = None, force_sms: bool = False) -> dict:
    # force_sms в новых Telethon НЕ работает — игнорируем, но честно пишем
    cp = cfg.load_settings()
    api_id = cfg.get(cp, "telegram", "api_id")
    api_hash = cfg.get(cp, "telegram", "api_hash")
    phone = (phone or cfg.get(cp, "telegram", "phone") or "").strip()
    if not api_id or not api_hash:
        return {"ok": False, "error": "Нет api_id / api_hash"}
    if not phone:
        return {"ok": False, "error": "Укажите телефон (+...) в настройках и сохраните"}
    if not phone.startswith("+"):
        return {
            "ok": False,
            "error": "Телефон должен быть в международном формате, например +79001234567",
        }

    if not cp.has_section("telegram"):
        cp.add_section("telegram")
    cp.set("telegram", "phone", phone)
    cfg.save_settings(cp)

    if force_sms:
        return {
            "ok": False,
            "error": (
                "SMS через Telegram API больше недоступен (Telethon: force_sms deprecated). "
                "Нажмите «Подключить готовую сессию» или ищите код в чате «Telegram» в приложении."
            ),
        }

    async def _send():
        client = await _client(int(api_id), api_hash)
        await client.connect()
        if await client.is_user_authorized():
            await client.disconnect()
            return {"ok": True, "already": True, "message": "Уже авторизованы"}

        result = await client.send_code_request(phone)
        _STATE["phone"] = phone
        _STATE["phone_code_hash"] = result.phone_code_hash
        _STATE["need_password"] = False
        tname = type(result.type).__name__
        await client.disconnect()

        if "App" in tname:
            where = (
                "Telegram принял запрос. Код должен быть в приложении Telegram "
                "в чате «Telegram» (официальный аккаунт), не в SMS. "
                "Если сообщения нет — нажмите «Подключить готовую сессию» "
                "(у вас уже есть вход в этом проекте)."
            )
            how = "app"
        elif "Sms" in tname:
            where = "Код отправлен по SMS."
            how = "sms"
        else:
            where = f"Код запрошен способом {tname}. Проверьте Telegram."
            how = tname

        return {"ok": True, "already": False, "delivery": how, "message": where}

    try:
        return _run(_send())
    except Exception as e:
        err = str(e)
        low = err.lower()
        if "flood" in low or "wait" in low:
            err = f"Слишком много запросов кода. Подождите. ({e})"
        elif "unavailable" in low:
            err = (
                "Telegram отказал в повторной отправке кода. "
                "Нажмите «Подключить готовую сессию» или подождите несколько часов. "
                f"({e})"
            )
        return {"ok": False, "error": err}


def submit_code(code: str) -> dict:
    code = (code or "").strip().replace(" ", "")
    if not code:
        return {"ok": False, "error": "Введите код из Telegram"}
    if not _STATE.get("phone_code_hash") or not _STATE.get("phone"):
        return {
            "ok": False,
            "error": "Сначала «Прислать код» или лучше «Подключить готовую сессию».",
        }

    cp = cfg.load_settings()
    api_id = int(cfg.get(cp, "telegram", "api_id"))
    api_hash = cfg.get(cp, "telegram", "api_hash")
    phone = _STATE["phone"]
    phone_code_hash = _STATE["phone_code_hash"]

    async def _sign():
        from telethon.errors import SessionPasswordNeededError

        client = await _client(api_id, api_hash)
        await client.connect()
        try:
            await client.sign_in(phone=phone, code=code, phone_code_hash=phone_code_hash)
        except SessionPasswordNeededError:
            _STATE["need_password"] = True
            await client.disconnect()
            return {
                "ok": False,
                "need_password": True,
                "error": "Нужен облачный пароль Telegram (2FA).",
            }
        ok = await client.is_user_authorized()
        await client.disconnect()
        _STATE["phone_code_hash"] = None
        _STATE["need_password"] = False
        return {"ok": ok, "message": "Вход выполнен. Можно «Скачать сигналы»."}

    try:
        return _run(_sign())
    except Exception as e:
        return {"ok": False, "error": str(e)}


def submit_password(password: str) -> dict:
    password = password or ""
    if not password:
        return {"ok": False, "error": "Введите облачный пароль Telegram"}

    cp = cfg.load_settings()
    api_id = int(cfg.get(cp, "telegram", "api_id"))
    api_hash = cfg.get(cp, "telegram", "api_hash")

    async def _pw():
        client = await _client(api_id, api_hash)
        await client.connect()
        await client.sign_in(password=password)
        ok = await client.is_user_authorized()
        await client.disconnect()
        _STATE["need_password"] = False
        _STATE["phone_code_hash"] = None
        return {"ok": ok, "message": "Вход выполнен (2FA). Можно «Скачать сигналы»."}

    try:
        return _run(_pw())
    except Exception as e:
        return {"ok": False, "error": str(e)}


def ensure_authorized() -> None:
    st = status()
    if st.get("authorized"):
        return
    # автопопытка импорта
    imp = import_existing_session()
    if imp.get("ok"):
        st2 = status()
        if st2.get("authorized"):
            return
    raise ValueError(
        "Нет входа в Telegram. В панели нажмите «Подключить готовую сессию» "
        "или войдите кодом из чата «Telegram». "
        "SMS через API больше не поддерживается."
    )
