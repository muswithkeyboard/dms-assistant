"""Контраст ключевых пар цветов в обеих темах (ревизия w3-ui-fix, WCAG 2.1 AA): текст ≥ 4,5:1, фокус и рамки
полей ≥ 3:1. Значения берутся из токенов static/app.css (светлая — :root, тёмная — :root[data-theme="dark"]);
полупрозрачные цвета смешиваются с фоном, на котором лежат. Таблица печатается: `uv run pytest -q -s tests/ui/test_contrast.py`."""
from __future__ import annotations

import re

from helpers import ROOT

CSS = (ROOT / "src" / "dms_api" / "static" / "app.css").read_text(encoding="utf-8")

# (передний план, фон, минимум, где встречается)
PAIRS = [
    ("--text", "--bg", 4.5, "основной текст на фоне страницы"),
    ("--text-2", "--bg", 4.5, "вторичный текст на фоне страницы"),
    ("--text-2", "--surface", 4.5, "вторичный текст в карточке"),
    ("--primary-text", "--surface", 4.5, "ссылка, вторичная кнопка, активная вкладка"),
    ("--primary-text", "--surface-2", 4.5, "ссылка на сером фоне"),
    ("--primary-text", "--primary-soft", 4.5, "сноска .cite"),
    ("--on-primary", "--primary-fill", 4.5, "основная кнопка, «Чат» в шапке, номер шага"),
    ("--on-primary", "--primary-fill-hover", 4.5, "основная кнопка при наведении"),
    ("--disabled-text", "--disabled-bg", 4.5, "неактивная кнопка"),
    ("--ok", "--ok-bg", 4.5, "чип «Можно»"),
    ("--warn", "--warn-bg", 4.5, "чип «При условиях», плашка черновика"),
    ("--danger", "--danger-bg", 4.5, "чип «Нельзя», ошибка"),
    ("--on-brand-2", "--brand-deep", 4.5, "подзаголовок и подвал"),
    ("--focus", "--surface", 3.0, "рамка фокуса в карточке"),
    ("--focus", "--bg", 3.0, "рамка фокуса на фоне страницы"),
    ("--focus-on-brand", "--brand-deep", 3.0, "рамка фокуса в шапке"),
    ("--input-line", "--surface", 3.0, "рамка поля ввода"),
    ("--bubble-user-fg", "--bubble-user-bg", 4.5, "реплика менеджера в чате (w15-ui)"),
    ("--bubble-user-meta", "--bubble-user-bg", 4.5, "время под репликой менеджера"),
]


def _block(selector: str) -> dict[str, str]:
    m = re.search(re.escape(selector) + r"\s*\{(.*?)\n\}", CSS, re.DOTALL)
    assert m, selector
    return dict(re.findall(r"(--[\w-]+):\s*([^;]+);", m.group(1)))


def _themes() -> dict[str, dict[str, str]]:
    light = _block(":root")
    dark = {**light, **_block(':root[data-theme="dark"]')}
    return {"light": light, "dark": dark}


def _rgba(v: str) -> tuple[float, float, float, float]:
    v = v.strip()
    if v.startswith("#"):
        h = v[1:]
        return int(h[0:2], 16) / 255, int(h[2:4], 16) / 255, int(h[4:6], 16) / 255, 1.0
    m = re.fullmatch(r"rgba?\(([^)]*)\)", v)
    assert m, v
    parts = [float(x) for x in m.group(1).split(",")]
    a = parts[3] if len(parts) == 4 else 1.0
    return parts[0] / 255, parts[1] / 255, parts[2] / 255, a


def _over(fg: tuple, bg: tuple) -> tuple[float, float, float]:
    a = fg[3]
    return tuple(f * a + b * (1 - a) for f, b in zip(fg[:3], bg[:3], strict=True))


def _lum(c: tuple) -> float:
    def f(v: float) -> float:
        return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4
    r, g, b = map(f, c[:3])
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def ratio(theme: dict[str, str], fg: str, bg: str) -> float:
    # полупрозрачный фон лежит на карточке (--surface); полупрозрачный текст — на своём фоне
    base = _rgba(theme["--surface"])
    b = _over(_rgba(theme[bg]), base)
    f = _over(_rgba(theme[fg]), (*b, 1.0))
    la, lb = _lum(f), _lum(b)
    return (max(la, lb) + 0.05) / (min(la, lb) + 0.05)


def contrast_table() -> list[tuple[str, str, str, float, float, str]]:
    out = []
    for name, theme in _themes().items():
        for fg, bg, need, where in PAIRS:
            out.append((name, fg, bg, round(ratio(theme, fg, bg), 2), need, where))
    return out


def test_key_pairs_contrast_both_themes():
    rows = contrast_table()
    for name, fg, bg, r, need, where in rows:
        print(f"{name:5} {fg:22} / {bg:20} {r:5.2f} (≥ {need}) — {where}")
    bad = [(n, fg, bg, r) for n, fg, bg, r, need, _ in rows if r < need]
    assert not bad, bad


def test_dark_media_block_matches_dark_theme():
    """@media (prefers-color-scheme: dark) повторяет значения :root[data-theme="dark"] (без расхождений тем)."""
    dark = _block(':root[data-theme="dark"]')
    m = re.search(r':root:not\(\[data-theme="light"\]\)\s*\{(.*?)\n  \}', CSS, re.DOTALL)
    assert m
    media = dict(re.findall(r"(--[\w-]+):\s*([^;]+);", m.group(1)))
    diff = {k: (v, media.get(k)) for k, v in dark.items() if media.get(k) != v}
    assert not diff, diff


def test_radii_via_tokens():
    """Радиусы в компонентах — только токенами (у Mains углы прямые): литералов px/% в border-radius нет."""
    body = CSS.split("*, *::before, *::after", 1)[1]
    found = [v for v in re.findall(r"border-radius:\s*([^;}]+)", body) if re.search(r"\d(px|%|em|rem)", re.sub(r"var\([^)]*\)", "", v))]
    assert not found, found


def test_disabled_button_not_transparent():
    """Неактивная кнопка — свои цвета и cursor: not-allowed, без opacity."""
    m = re.search(r"\.btn\[disabled\][^{]*\{([^}]*)\}", CSS)
    assert m and "opacity" not in m.group(1) and "not-allowed" in m.group(1)
    assert "var(--disabled-text)" in m.group(1) and "var(--disabled-bg)" in m.group(1)
