"""Превью источника у ссылки в ответе (этап w17-preview): статические проверки chat.js, app.css и ui.yaml.
Поведение в браузере проверялось вручную (отчёт docs/status/stage-w17-preview.md)."""
from __future__ import annotations

import re

from dms_core.config import load_yaml
from helpers import ROOT

STATIC = ROOT / "src" / "dms_api" / "static"


def _code(name: str) -> str:
    src = (STATIC / name).read_text(encoding="utf-8")
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.DOTALL)
    return re.sub(r"(^|[^:\"'])//[^\n]*", r"\1", src)


def _fn(code: str, name: str) -> str:
    m = re.search(r"\n  function " + name + r"\(.*?(?=\n  function |\n  var [A-Z_]+ =|\Z)", code, re.DOTALL)
    assert m, name
    return m.group(0)


def test_preview_texts_in_ui_yaml():
    ui = load_yaml("texts_ru/ui.yaml")
    for key in ("label", "verified", "loading", "context", "hint", "open", "close"):
        assert isinstance(ui["preview"][key], str) and ui["preview"][key], key
    code = _code("chat.js")
    for key in re.findall(r'T\("preview\.(\w+)"', code):
        assert key in ui["preview"], key


def test_all_source_links_use_preview():
    """Ссылка «Источник:» в ответе, сноски [n] и список «Источники» — через sourceLink (клик + превью)."""
    code = _code("chat.js")
    assert 'sourceLink("reply__ref"' in _fn(code, "pointItem")
    assert 'sourceLink("cite"' in _fn(code, "citeButtons")
    assert 'sourceLink("srclist__btn"' in _fn(code, "detailBlocks")
    # прежних обработчиков клика в обход превью не осталось (кроме шторки и клиник)
    assert "openSource(sources, n - 1" not in code and "openSource(sources, i," not in code
    assert code.count("openSource(sources, idx, b)") == 2   # sourceClick и кнопка «Открыть источник» в превью


def test_preview_lazy_delay_and_cache():
    code = _code("chat.js")
    assert re.search(r"PREVIEW_DELAY = 250\b", code)
    sched = _fn(code, "schedulePreview")
    assert "setTimeout" in sched and "PREVIEW_DELAY" in sched and "loadClause" not in sched
    show = _fn(code, "showPreview")
    # текст пункта — лениво, тем же loadClause (кэш state.clauseCache), устаревший ответ не показывается
    assert "loadClause(c)" in show and "tok !== pv.token" in show
    load = _fn(code, "loadClause")
    assert "state.clauseCache[key]" in load and "clausePending[key]" in load
    # при ошибке остаётся цитата: блок цитаты прячется только когда нашёлся кусок текста пункта
    assert "if (ex)" in show and "quoteBox.hidden = true" in show


def test_preview_a11y_and_dismiss():
    code = _code("chat.js")
    show = _fn(code, "showPreview")
    assert '"tooltip"' in show and '"dialog"' in show and '"aria-describedby", "src-preview"' in show
    assert 'T("preview.open")' in show
    link = _fn(code, "sourceLink")
    assert ":focus-visible" in link and '"pointerenter"' in link and '"blur"' in link
    assert 'e.pointerType === "mouse"' in link
    # Esc закрывает превью раньше шторки
    wire = _fn(code, "wire")
    assert wire.index("previewOpen()") < wire.index('$("sources").classList.contains("is-open")')
    assert '"scroll"' in wire and '"pointerdown"' in wire
    # тап: первый — превью, повторный по той же ссылке — шторка
    click = _fn(code, "sourceClick")
    assert '"touch"' in click and "showPreview(b, c, sources, idx, true)" in click


def test_preview_stays_on_screen():
    place = _fn(_code("chat.js"), "placePreview")
    assert "innerHeight" in place and "clientWidth" in place
    assert "is-up" in place and "r.right - w" in place and "maxHeight" in place


def test_preview_css_tokens_only():
    css = (STATIC / "app.css").read_text(encoding="utf-8")
    rules = re.findall(r"\.srcprev[\w-]*[^{]*\{([^}]*)\}", css)
    assert rules
    for body in rules:
        assert not re.search(r"#[0-9A-Fa-f]{3,8}\b|rgba?\(", body), body
    assert re.search(r"\.srcprev \{[^}]*position: fixed", css)
    # ссылка на источник подчёркнута как гиперссылка
    assert re.search(r"\.reply__reft \{[^}]*text-decoration: underline", css)
    assert re.search(r"\.srclist__doc \{[^}]*text-decoration: underline", css)


def test_understood_line_rendered_from_reply():
    """ADR 0015: как модель-помощник поняла вопрос — строка над ответом, текст подписи из ui.yaml."""
    from pathlib import Path

    js = (Path(__file__).resolve().parents[2] / "src/dms_api/static/chat.js").read_text(encoding="utf-8")
    assert "r.understood" in js and 'T("reply.understood")' in js


def test_service_message_has_no_status_line():
    """Служебное сообщение (загрузка вложения, «привет») — без строки статуса ТЗ п. 8.1 (ChatReply.service)."""
    from pathlib import Path

    js = (Path(__file__).resolve().parents[2] / "src/dms_api/static/chat.js").read_text(encoding="utf-8")
    assert "a.confidence && !r.service" in js
