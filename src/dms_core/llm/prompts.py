"""Промпты из config/prompts/<name>.md (`docs/rules.md` п. 5): текст и версия из шапки «<!-- version: X -->».

Промпты — конфигурация, в коде их нет. Версия идёт в журнал прогона и в ответ (воспроизводимость сравнения моделей).
"""
from __future__ import annotations

import re
from functools import cache

from .. import config

_VERSION_RE = re.compile(r"^\s*<!--\s*version:\s*([^\s>]+)\s*-->\s*$")


@cache
def _load(path: str) -> tuple[str, str]:
    from pathlib import Path

    raw = Path(path).read_text(encoding="utf-8")
    lines = raw.splitlines()
    m = _VERSION_RE.match(lines[0]) if lines else None
    if not m:
        return raw.strip(), "unknown"
    return "\n".join(lines[1:]).strip(), m.group(1)


def load(name: str) -> tuple[str, str]:
    """(текст без шапки, версия). Нет файла — FileNotFoundError: без промпта модель не вызывается."""
    if not re.fullmatch(r"[A-Za-z0-9_\-]+", name):
        raise ValueError(f"недопустимое имя промпта: {name!r}")
    return _load(str(config.CONFIG_DIR / "prompts" / f"{name}.md"))
