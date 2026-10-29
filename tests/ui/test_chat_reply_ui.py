"""Чат как мессенджер (этап w15-ui, ADR 0014, CONTRACTS §12): ответ словами (Answer.reply) рисуется пузырём —
метка и lead, «Что сказано в договоре» (текст, цитата, строка источника со сноской), «Что уточнить», статус мелко;
служебное — под «Подробнее» (скрыто по умолчанию); без reply — прежняя карточка; id строк на экран не попадают."""
from __future__ import annotations

import re

from dms_core import constants as K
from dms_core.config import load_yaml
from helpers import ROOT, client

TPL = ROOT / "src" / "dms_api" / "templates"
STATIC = ROOT / "src" / "dms_api" / "static"


def _ui() -> dict:
    return load_yaml("texts_ru/ui.yaml")


def _code(name: str) -> str:
    src = (STATIC / name).read_text(encoding="utf-8")
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.DOTALL)
    return re.sub(r"(^|[^:\"'])//[^\n]*", r"\1", src)


def _fn(code: str, name: str) -> str:
    """Тело функции name (до следующего объявления function на том же уровне отступа)."""
    m = re.search(r"\n  function " + name + r"\(.*?(?=\n  function |\n  var [A-Z_]+ =|\Z)", code, re.DOTALL)
    assert m, name
    return m.group(0)


def test_ui_yaml_reply_keys():
    """Тексты ответа словами, меню «⋯», «печатает…» и компактного выбора клиники — в ui.yaml."""
    ui = _ui()
    rep = ui["reply"]
    for key in ("points_title", "points_title_group", "ask_title", "ask_use", "source", "more", "less", "status",
                "contract", "table_row", "sheet_row"):
        assert isinstance(rep.get(key), str) and rep[key], f"reply.{key}"
    # метки одной строкой — по всем значениям Label и два своих случая
    for v in [e.value for e in K.Label] + ["clarify", "mixed"]:
        assert isinstance(rep["verdict"].get(v), str), f"reply.verdict.{v}"
    for key in ("menu", "menu_new", "menu_close", "menu_reopen", "menu_rate", "menu_closed_on", "menu_closed_off",
                "menu_sources", "menu_add_clinic", "send", "examples_btn", "suggest_title", "typing_aria", "new_answer",
                "head_contract", "chats_toggle", "closed_bar"):
        assert isinstance(ui["chat"].get(key), str), f"chat.{key}"
    # «печатает…» — коротко по каждой стадии SSE сервиса (dms_api.service.STAGES) и запасная строка
    from dms_api.service import STAGES
    for st in list(STAGES) + ["start", "other"]:
        t = ui["chat"]["typing"].get(st)
        assert isinstance(t, str) and len(t) <= 40, f"chat.typing.{st}"
    for key in ("question", "other", "other_hide"):
        assert isinstance(ui["clinic_pick"].get(key), str), f"clinic_pick.{key}"
    assert "{id}" not in ui["clinic_pick"]["selected_unknown"]       # id строки перечня не показываем
    assert "{t}" in rep["table_row"] and "{r}" in rep["table_row"]


def test_reply_bubble_markup():
    """replyBubble рисует: метку + lead, заголовок пунктов, у пункта — текст, цитату курсивом и строку источника
    с кнопкой-сноской (открывает шторку), «Что уточнить», статус, «Подробнее» с aria-expanded/aria-controls."""
    code = _code("chat.js")
    body = _fn(code, "replyBubble")
    for part in ('"reply__lead"', "verdictChip(verdictOf(a))", "r.lead", '"reply.points_title"', '"reply.ask_title"',
                 '"reply__foot"', '"reply.status"', "r.caution", 'reply__morebtn"', '"aria-expanded"', '"aria-controls"',
                 "detailBlocks(a, sources, { reply: true })", "clinicPickCard(a, isLast, msgIdx, true)"):
        assert part in body, part
    pt = _fn(code, "pointItem")
    # ссылка источника — sourceLink (w17-preview): клик открывает шторку, наведение/фокус — превью
    for part in ('"reply__text"', '"reply__quote"', '"reply__src"', '"reply__ref"', "sourceLink(\"reply__ref\", c, sources, n - 1",
                 "docTitle(c)", "citeWhere(c)"):
        assert part in pt, part
    assert "openSource(sources, idx, b)" in _fn(code, "sourceClick")
    # обратная совместимость: без reply — прежняя карточка
    assert "m.answer.reply ? replyBubble(" in code and ": answerCard(m.answer" in code
    # новый ответ объявляется коротко в aria-live, лента — role=log
    assert 'T("chat.new_answer"' in code and '$("announce")' in code


def test_details_hidden_by_default():
    """«Подробнее» скрыт, пока менеджер его не раскрыл (state.detailsOpen); кнопка — aria-expanded=false."""
    body = _fn(_code("chat.js"), "replyBubble")
    assert "var open = !!state.detailsOpen[key];" in body
    assert 'hidden: !open' in body and '"aria-expanded": open ? "true" : "false"' in body
    assert re.search(r"detailsOpen:\s*\{\}", _code("chat.js"))


def test_point_cites_resolve_refs_and_quote():
    """Сноска пункта находит проверенную Citation: по ref (Citation.ref / table_ref / «документ:пункт»), при
    нескольких — по цитате пункта; без совпадения ref — по цитате; чужой договор (ГК) не берётся."""
    code = _code("chat.js")
    rm = _fn(code, "refMatch")
    assert "c.ref === r" in rm and "c.table_ref === r" in rm and "c.clause_number" in rm
    pc = _fn(code, "pointCites")
    assert "p.contract_id" in pc and "c.contract_id === p.contract_id" in pc and "quoted" in pc


def test_no_row_ids_on_screen():
    """id строк («c06.program#t5r250», «Лист1!B57») не показываются: ссылка на строку — словами (humanRef);
    в шаблонах нет таких id; альтернативы — список без «Основание» и source_ref."""
    code = _code("chat.js")
    hr = _fn(code, "humanRef")
    assert '"reply.table_row"' in hr and '"reply.sheet_row"' in hr and 'return "";' in hr
    cw = _fn(code, "citeWhere")
    assert "humanRef(c.table_ref)" in cw and '"answer.table"' not in cw
    alt = _fn(code, "alternativesCard")
    assert "source_ref" not in alt and "alt_reason" not in alt and "<table" not in alt and '"table"' not in alt
    assert "humanRef(row.source_ref)" in _fn(code, "clinicRow")
    for p in TPL.glob("*.html"):
        src = p.read_text(encoding="utf-8")
        assert not re.search(r"#t\d+r\d+|\b[a-z]\d{2}\.[a-z]+#|!\$?[A-Z]{1,3}\d+", src), p.name


def test_chat_page_layout():
    """Страница чата: две колонки, «Новый чат» сверху, обращения скрыты по умолчанию, меню «⋯», лента role=log,
    aria-live для нового ответа, «Примеры вопросов» над полем ввода, «Отправить»; шторка источников."""
    r = client().get("/")
    assert r.status_code == 200
    t = r.text
    for part in ('id="chatlay"', 'id="new-chat-btn"', 'id="req-box"', 'id="chat-menu-btn"', 'aria-haspopup="true"',
                 'role="log"', 'id="announce"', 'aria-live="polite"', 'id="examples-btn"', 'aria-controls="examples"',
                 'id="ask-btn"', 'id="sources"', 'id="side-toggle"'):
        assert part in t, part
    assert re.search(r'<section class="side__req" id="req-box"[^>]*hidden', t)
    assert re.search(r'<div class="examples" id="examples" hidden', t)
    assert 'id="chat-ctx"' not in t                                  # шапка — одна строка, без таблицы контекста
    assert t.index('id="new-chat-btn"') < t.index('id="chat-list"')   # «Новый чат» — сверху


def test_enter_sends_shift_enter_newline():
    """Enter — отправка, Shift+Enter — перенос строки, Ctrl/Cmd+Enter — тоже отправка; во время IME — не трогаем."""
    code = _code("chat.js")
    assert 'if (e.key !== "Enter" || e.isComposing || e.shiftKey) { return; }' in code
    assert 'e.preventDefault(); ask($("question").value);' in code


def test_typing_indicator_short_stages():
    """Индикатор — пузырь «печатает…» с одной строкой из chat.typing.<стадия>; технических стадий в тексте нет."""
    body = _fn(_code("chat.js"), "progressNode")
    assert '"chat.typing." + name' in body and '"chat.typing.other"' in body and "typing__dots" in body
    assert "chat.stages" not in body


def test_mock_answers_carry_reply():
    """Демо-API отдаёт reply по CONTRACTS §12 (lead, points[text, quote, refs, contract_id], ask, caution) и умеет
    без него (&noreply=1 — прежний вид); цитата пункта — из проверенной цитаты ссылки."""
    mock = (STATIC / "mock_api.js").read_text(encoding="utf-8")
    assert "function replyFor(" in mock and "a.reply = NOREPLY ? null : replyFor(text, a)" in mock
    assert re.search(r"points\.push\(\{ text: st\.text, quote: cs\[0\]\.quote, refs: cs\.map\(refOf\), contract_id:", mock)
    assert f"points: points.slice(0, {getattr(K, 'REPLY_POINTS_MAX', 6)})" in mock   # constants.REPLY_POINTS_MAX
    assert "noreply=1" in mock
