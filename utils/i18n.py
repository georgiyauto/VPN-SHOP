"""
Локализация — загрузка текстов из locales/ru.json и locales/en.json.
Использование:
    from utils.i18n import t
    text = t("welcome", lang=user.language, name="Иван")
"""
import json
from pathlib import Path

_LOCALES: dict[str, dict] = {}
_LOCALES_DIR = Path(__file__).parent.parent / "locales"
_SUPPORTED = ["ru", "en"]
_DEFAULT = "ru"


def _load():
    for lang in _SUPPORTED:
        path = _LOCALES_DIR / f"{lang}.json"
        if path.exists():
            with open(path, encoding="utf-8") as f:
                _LOCALES[lang] = json.load(f)


_load()


def t(key: str, lang: str = "ru", **kwargs) -> str:
    """Получить текст по ключу для нужного языка с подстановкой переменных."""
    if lang not in _LOCALES:
        lang = _DEFAULT
    text = _LOCALES[lang].get(key) or _LOCALES[_DEFAULT].get(key, key)
    if kwargs:
        try:
            text = text.format(**kwargs)
        except (KeyError, ValueError):
            pass
    return text


def detect_lang(tg_lang: str | None) -> str:
    """Определить язык по telegram language_code."""
    if tg_lang and tg_lang.startswith("en"):
        return "en"
    return "ru"
