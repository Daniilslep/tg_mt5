# -*- coding: utf-8 -*-
"""Чтение/запись единого файла НАСТРОЙКИ.ini"""

from __future__ import annotations

import configparser
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SETTINGS_PATH = ROOT / "НАСТРОЙКИ.ini"
EXAMPLE_PATH = ROOT / "НАСТРОЙКИ.пример.ini"
PRESETS_DIR = ROOT / "presets"
SESSION_PATH = ROOT / "tg_session"
OUTPUT_DIR = ROOT / "output"


def _cp() -> configparser.ConfigParser:
    cp = configparser.ConfigParser()
    cp.optionxform = str  # сохранить регистр ключей
    return cp


def ensure_settings() -> Path:
    if not SETTINGS_PATH.exists():
        if EXAMPLE_PATH.exists():
            shutil.copy(EXAMPLE_PATH, SETTINGS_PATH)
        else:
            SETTINGS_PATH.write_text(
                "[telegram]\nchannel=\napi_id=\napi_hash=\nphone=\n",
                encoding="utf-8",
            )
    return SETTINGS_PATH


def load_settings(path: Path | None = None) -> configparser.ConfigParser:
    ensure_settings()
    cp = _cp()
    p = path or SETTINGS_PATH
    text = p.read_text(encoding="utf-8-sig")  # utf-8-sig снимает BOM от Windows
    cp.read_string(text)
    return cp


def save_settings(cp: configparser.ConfigParser, path: Path | None = None) -> None:
    p = path or SETTINGS_PATH
    with p.open("w", encoding="utf-8") as f:
        cp.write(f)


def get(cp: configparser.ConfigParser, section: str, key: str, default: str = "") -> str:
    if not cp.has_section(section):
        return default
    return cp.get(section, key, fallback=default).strip()


def get_int(cp: configparser.ConfigParser, section: str, key: str, default: int) -> int:
    try:
        return int(get(cp, section, key, str(default)) or default)
    except ValueError:
        return default


def get_float(cp: configparser.ConfigParser, section: str, key: str, default: float) -> float:
    try:
        return float(get(cp, section, key, str(default)) or default)
    except ValueError:
        return default


def apply_preset(cp: configparser.ConfigParser, preset_name: str) -> None:
    """Подставить секции [parse]/[manage] из presets/<name>.ini"""
    name = preset_name.strip().lower().replace(" ", "")
    path = PRESETS_DIR / f"{name}.ini"
    if not path.exists():
        raise FileNotFoundError(f"Пресет не найден: {path}")
    preset = _cp()
    preset.read(path, encoding="utf-8")
    if not cp.has_section("parse"):
        cp.add_section("parse")
    if preset.has_section("parse"):
        for k, v in preset.items("parse"):
            cp.set("parse", k, v)
    if preset.has_section("manage"):
        if not cp.has_section("manage"):
            cp.add_section("manage")
        for k, v in preset.items("manage"):
            cp.set("manage", k, v)
    if preset.has_section("telegram") and preset.has_option("telegram", "channel"):
        if not cp.has_section("telegram"):
            cp.add_section("telegram")
        # канал из пресета — только если пустой
        if not get(cp, "telegram", "channel"):
            cp.set("telegram", "channel", preset.get("telegram", "channel"))


def as_dict(cp: configparser.ConfigParser) -> dict:
    out: dict = {}
    for sec in cp.sections():
        out[sec] = {k: v for k, v in cp.items(sec)}
    return out


def mt5_common_files() -> Path | None:
    import os

    appdata = os.environ.get("APPDATA")
    if not appdata:
        return None
    p = Path(appdata) / "MetaQuotes" / "Terminal" / "Common" / "Files"
    return p if p.exists() else None


def export_live_rules(cp: configparser.ConfigParser) -> Path | None:
    """Пишет правила для Live EA в Common/Files/SignalKit/parse_rules.txt"""
    common = mt5_common_files()
    if common is None:
        dest = OUTPUT_DIR / "parse_rules.txt"
    else:
        dest = common / "SignalKit" / "parse_rules.txt"
    dest.parent.mkdir(parents=True, exist_ok=True)

    channel = get(cp, "telegram", "channel").lstrip("@")
    def g(sec: str, key: str, default: str = "") -> str:
        return get(cp, sec, key, default)

    lines = [
        f"channel={channel}",
        f"format={g('parse', 'format', 'labels')}",
        f"must_contain={g('parse', 'must_contain')}",
        f"skip_if_contains={g('parse', 'skip_if_contains')}",
        f"label_side={g('parse', 'label_side')}",
        f"label_entry={g('parse', 'label_entry')}",
        f"label_sl={g('parse', 'label_sl')}",
        f"label_tp={g('parse', 'label_tp')}",
        f"buy_words={g('parse', 'buy_words')}",
        f"sell_words={g('parse', 'sell_words')}",
        f"limit_words={g('parse', 'limit_words')}",
        f"tp_open_words={g('parse', 'tp_open_words')}",
        f"open_tp_rr={g('parse', 'open_tp_rr', '2.0')}",
        f"symbol_from_hashtag={g('parse', 'symbol_from_hashtag', 'yes')}",
        f"compact_side_buy={g('parse', 'compact_side_buy', 'Buy')}",
        f"compact_side_sell={g('parse', 'compact_side_sell', 'Sell')}",
        f"compact_sl_word={g('parse', 'compact_sl_word', 'SL')}",
        f"compact_tp_word={g('parse', 'compact_tp_word', 'TP')}",
        f"manage_enabled={g('manage', 'enabled', 'no')}",
        f"manage_link_hours={g('manage', 'link_max_hours', '720')}",
        f"manage_words_cancel={g('manage', 'words_cancel_pending')}",
        f"manage_words_reverse={g('manage', 'words_reverse')}",
        f"manage_words_be={g('manage', 'words_breakeven')}",
        f"manage_words_close={g('manage', 'words_close')}",
        f"manage_words_modify={g('manage', 'words_modify_sl')}",
        f"manage_words_market={g('manage', 'words_to_market')}",
        f"manage_words_inherit={g('manage', 'words_inherit_levels', 'не меняем|без изменений|параметры без|остальные параметры')}",
        f"manage_words_levels={g('manage', 'words_modify_levels', 'меняем цену|цену открытия на|сменили цен|переставляем лимит')}",
        f"manage_words_add={g('manage', 'words_add')}",
        f"manage_words_keep={g('manage', 'words_keep_pending')}",
    ]
    text = "\n".join(lines) + "\n"
    dest.write_text(text, encoding="utf-16")
    dest.with_suffix(".utf8.txt").write_text(text, encoding="utf-8")
    # копия в output проекта — удобно сверять
    (OUTPUT_DIR / "parse_rules.txt").write_text(text, encoding="utf-8")
    return dest
