"""Дефекты ревизии интерфейса (этап w16-ui): статические проверки кода страниц и текстов ui.yaml.
Поведение в браузере проверялось Playwright (отчёт docs/status/stage-w16-ui.md)."""
from __future__ import annotations

import re

from dms_core.config import load_yaml
from helpers import ROOT, client

STATIC = ROOT / "src" / "dms_api" / "static"


def _code(name: str) -> str:
    src = (STATIC / name).read_text(encoding="utf-8")
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.DOTALL)
    return re.sub(r"(^|[^:\"'])//[^\n]*", r"\1", src)


def _fn(code: str, name: str) -> str:
    m = re.search(r"\n  function " + name + r"\(.*?(?=\n  function |\n  var [A-Z_]+ =|\Z)", code, re.DOTALL)
    assert m, name
    return m.group(0)


def test_ui_yaml_w16_texts():
    ui = load_yaml("texts_ru/ui.yaml")
    for key in ("input_count", "input_limit", "input_cut"):
        assert "{max}" in ui["chat"][key], key
    for key in ("options_label", "prev_before_pick", "prev_show", "prev_hide"):
        assert isinstance(ui["reply"][key], str) and ui["reply"][key], key
    assert ui["docs"]["upload_by_placeholder"] == "оператор"
    # имена полей шаблонов писем — все по-русски (черновик «Заполните вручную: …»)
    fields = set(re.findall(r"\{([a-z_]+)\}", (ROOT / "config" / "texts_ru" / "drafts.yaml").read_text(encoding="utf-8")))
    assert fields <= set(ui["automation"]["field_names"]), fields - set(ui["automation"]["field_names"])


def test_chat_reopen_opens_last_request():
    """Без &req= открывается последнее открытое обращение (или последнее вообще), а не пустой чат."""
    body = _fn(_code("chat.js"), "selectChat")
    assert "status !== \"closed\"" in body and "status=all" in body


def test_no_null_suggestions_and_examples_btn():
    code = _code("chat.js")
    assert "box.appendChild(suggestions())" not in code
    assert "$(\"examples-btn\").hidden = !state.examples.length" in code


def test_ask_questions_vs_statements_and_trim():
    code = _code("chat.js")
    body = _fn(code, "replyBubble")
    # вопрос (есть «?») — блок с вариантами и «своим ответом» (clarifyItem), утверждение — заметка reply__note
    assert "reply__note" in body and 'q.indexOf("?")' in body and "clarifyItem(question, why)" in body
    item = _fn(code, "clarifyItem")
    assert "ask(o, question)" in item and "ask(v, question)" in item and '"reply.clarify_own"' in item
    assert "\\n" not in _fn(code, "useQuestion")
    assert ".trim()" in _fn(code, "useQuestion")
    # варианты ответа (ChatReply.options) — кнопки, клик отправляет вопрос сразу
    assert "r.options" in body and "ask(o.trim())" in body


def test_footnotes_stripped_and_ref_passed():
    code = _code("chat.js")
    assert "FOOTNOTE_RE" in code and "noFootnotes(s).replace" in _fn(code, "cleanText")
    # запрос пункта вынесен в loadClause (w17-preview): им пользуются и шторка, и превью ссылки
    load = _fn(code, "loadClause")
    assert "c.ref" in load and "\"ref=\"" in load
    assert "loadClause(c)" in _fn(code, "fragment")
    assert "state.hitRef" in _code("documents.js")


def test_tools_strip_not_shrinking():
    css = (STATIC / "app.css").read_text(encoding="utf-8")
    assert re.search(r"#request-tools \{[^}]*flex: none", css)
    assert not re.search(r"\.tools \{[^}]*max-height", css)


def test_pages_have_counter_and_placeholder():
    c = client()
    html = c.get("/").text
    assert 'id="ask-count"' in html
    docs = c.get("/documents").text
    assert 'placeholder="оператор"' in docs and 'value="operator"' not in docs
