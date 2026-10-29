"""Интерфейс (этап w3-ui): шаблоны и скрипты страниц без внешних ресурсов, inline-скриптов и стилей (CSP),
тексты — только из config/texts_ru/ui.yaml, вставка данных — только textContent, mailto: — только по клику,
демо-API — только флагом ?mock=1, кнопки автомата совпадают с config/add_clinic.yaml."""
from __future__ import annotations

import html
import json
import re
from pathlib import Path

from dms_core import constants as K
from dms_core.config import load_yaml
from helpers import ROOT, client

TPL = ROOT / "src" / "dms_api" / "templates"
STATIC = ROOT / "src" / "dms_api" / "static"
PAGE_JS = ["common.js", "chat.js", "documents.js", "clients.js", "theme.js", "settings.js"]   # скрипты страниц (mock_api.js — демо-данные)
CYR = re.compile(r"[А-Яа-яЁё]")


def _ui() -> dict:
    return load_yaml("texts_ru/ui.yaml")


def _templates() -> list[Path]:
    return sorted(TPL.glob("*.html"))


def _strip_js_comments(src: str) -> str:
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.DOTALL)
    return re.sub(r"(^|[^:\"'])//[^\n]*", r"\1", src)


def _lookup(d: dict, path: str):
    for k in path.split("."):
        if not isinstance(d, dict) or k not in d:
            return None
        d = d[k]
    return d


def test_templates_csp_safe_and_local():
    """Нет inline-скриптов, стилей и обработчиков on*=; нет внешних адресов и mailto в разметке."""
    for p in _templates():
        src = p.read_text(encoding="utf-8")
        assert not re.search(r"<script(?![^>]*\bsrc=)", src), f"{p.name}: inline <script>"
        assert "<style" not in src and not re.search(r"\sstyle=", src), f"{p.name}: inline-стиль"
        assert not re.search(r"\son[a-z]+=", src), f"{p.name}: обработчик on*="
        for bad in ("http://", "https://", "//cdn", "javascript:", "mailto:"):
            assert bad not in src, f"{p.name}: {bad}"


def test_templates_texts_only_from_ui_yaml():
    """Вне выражений Jinja в шаблонах нет русского текста: все подписи — {{ t.* }} из ui.yaml."""
    for p in _templates():
        src = p.read_text(encoding="utf-8")
        bare = re.sub(r"\{\{.*?\}\}|\{%.*?%\}|\{#.*?#\}", "", src, flags=re.DOTALL)
        found = CYR.findall(bare)
        assert not found, f"{p.name}: русский текст вне ui.yaml: {''.join(found[:40])}"


def test_page_js_texts_only_from_ui_yaml():
    """В скриптах страниц (без комментариев) нет русских строк — тексты берутся через T("путь")."""
    for name in PAGE_JS:
        code = _strip_js_comments((STATIC / name).read_text(encoding="utf-8"))
        found = CYR.findall(code)
        assert not found, f"{name}: русский текст в коде: {''.join(found[:40])}"


def test_js_text_keys_exist():
    """Каждый T("путь") существует в ui.yaml; префиксы T("путь." + x) — разделы ui.yaml."""
    ui = _ui()
    for name in ("common.js", "chat.js", "documents.js", "clients.js", "settings.js"):
        code = (STATIC / name).read_text(encoding="utf-8")
        for key in re.findall(r"\bT\(\"([a-z_.]+)\"\s*[,)]", code):
            assert isinstance(_lookup(ui, key), str), f"{name}: нет текста {key}"
        for prefix in re.findall(r"\bT\(\"([a-z_.]+)\.\"\s*\+", code):
            assert isinstance(_lookup(ui, prefix), dict), f"{name}: нет раздела {prefix}"
        for key in re.findall(r"\bcfg\(\"([a-z_.]+)\"\)", code):
            assert _lookup(ui, key) is not None, f"{name}: нет раздела {key}"


def test_no_html_injection_in_js():
    """Данные сервера вставляются только как текст: ни innerHTML, ни document.write, ни eval."""
    for p in STATIC.glob("*.js"):
        code = _strip_js_comments(p.read_text(encoding="utf-8"))
        for bad in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "new Function"):
            assert bad not in code, f"{p.name}: {bad}"
        assert "http://" not in code and "https://" not in code, f"{p.name}: внешний адрес"


def test_enums_have_texts():
    """У каждого значения меток, статусов, состояний автомата, типов документов и режимов есть подпись."""
    ui = _ui()
    for section, enum in (("labels", K.Label), ("confidence", K.Confidence), ("doc_status", K.DocStatus),
                          ("doc_types", K.DocType), ("insurers", K.Insurer), ("modes", K.AnswerMode),
                          ("scenarios", K.Scenario), ("statement_kinds", K.StatementKind)):
        missing = [e.value for e in enum if e.value not in ui[section]]
        assert not missing, f"ui.yaml {section}: нет {missing}"
    missing = [e.value for e in K.AddClinicState if e.value not in ui["automation"]["states"]]
    assert not missing, f"ui.yaml automation.states: нет {missing}"


def test_automation_buttons_match_add_clinic_config():
    """Кнопки следующего действия — ровно переходы config/add_clinic.yaml; у каждого действия есть подпись."""
    ui, cfg = _ui()["automation"], load_yaml("add_clinic.yaml")
    flow = {k: sorted(v) for k, v in ui["flow"].items()}
    assert flow == {k: sorted(v) for k, v in cfg["transitions"].items()}
    actions = {a for v in cfg["transitions"].values() for a in v}
    assert actions <= set(ui["actions"]), actions - set(ui["actions"])
    # поле «Ответ СК» — ровно у действий, которые фиксируют ответ СК в автомате
    from dms_core.scenarios.add_clinic import INSURER_ACTIONS

    assert sorted(ui["reply_actions"]) == sorted(INSURER_ACTIONS)
    assert ui["actions"]["insurer_confirmed"] == "СК подтвердила добавление"


def test_mock_api_flow_matches_add_clinic_config():
    """Демо-API (?mock=1) идёт по тем же переходам и черновикам, что и сервер (config/add_clinic.yaml)."""
    cfg = load_yaml("add_clinic.yaml")
    code = (STATIC / "mock_api.js").read_text(encoding="utf-8")
    flow = json.loads(re.search(r"/\* flow:begin \*/(.*?)/\* flow:end \*/", code, re.DOTALL).group(1))
    drafts = json.loads(re.search(r"/\* drafts:begin \*/(.*?)/\* drafts:end \*/", code, re.DOTALL).group(1))
    assert flow == cfg["transitions"]
    assert drafts == cfg["drafts"]


def test_step_path_has_confirmation_after_hr_agreed():
    """Полоса шагов: в ветке «платно» после «HR согласовал» идёт «СК подтвердила добавление»."""
    code = (STATIC / "chat.js").read_text(encoding="utf-8")
    assert 'path.push("hr_agreed"); path.push("insurer_confirmed");' in code


def test_mailto_only_in_click_handler():
    """Письмо не уходит само: mailto: собирается только в openMail, а openMail вызывается только по клику."""
    chat = _strip_js_comments((STATIC / "chat.js").read_text(encoding="utf-8"))
    assert chat.count("mailto:") == 1
    m = re.search(r"function openMail\([^)]*\)\s*\{(.*?)\n  \}", chat, flags=re.DOTALL)
    assert m and "mailto:" in m.group(1)
    calls = [line for line in chat.splitlines() if "openMail(" in line and "function openMail" not in line]
    assert calls and all("click" in line for line in calls), calls
    for p in STATIC.glob("*.js"):
        if p.name != "chat.js":
            assert "mailto:" not in _strip_js_comments(p.read_text(encoding="utf-8")), p.name
    # Сервер не отправляет письма (`docs/rules.md` п. 3): в коде сервиса нет SMTP и почтовых библиотек.
    for p in (ROOT / "src").rglob("*.py"):
        src = p.read_text(encoding="utf-8").lower()
        assert "smtplib" not in src and "sendmail" not in src, p


def test_mock_api_only_by_flag():
    """Демо-API не подключается шаблонами; common.js грузит его только при ?mock=1."""
    for p in _templates():
        assert "mock_api" not in p.read_text(encoding="utf-8"), p.name
    common = (STATIC / "common.js").read_text(encoding="utf-8")
    assert re.search(r"var MOCK = .*mock=1", common)
    assert re.search(r"if \(!MOCK\) \{ resolve\(\); return; \}", common)
    assert "/static/mock_api.js" in common
    r = client().get("/")
    assert "mock_api" not in r.text


def test_pages_carry_ui_texts_and_scripts():
    """Страницы отдают тексты ui.yaml в data-ui (JSON) и подключают свои скрипты; статика доступна."""
    c = client()
    ui = _ui()
    for path, script in (("/", "chat.js"), ("/documents", "documents.js")):
        r = c.get(path)
        assert r.status_code == 200
        m = re.search(r"data-ui='([^']*)'", r.text)
        assert m, path
        assert json.loads(html.unescape(m.group(1))) == ui
        assert f"/static/{script}" in r.text and "/static/common.js" in r.text
        assert 'id="main"' in r.text and 'class="skip"' in r.text   # skip-link
    for name in PAGE_JS + ["mock_api.js", "app.css"]:
        assert c.get(f"/static/{name}").status_code == 200, name


def test_css_colors_only_via_tokens():
    """В компонентах app.css цвета — только через токены: литералы цвета допустимы лишь в блоках тем."""
    css = (STATIC / "app.css").read_text(encoding="utf-8")
    body = css.split("*, *::before, *::after", 1)[1]   # всё после определения токенов тем
    found = re.findall(r"#[0-9a-fA-F]{3,8}\b|rgba?\(", body)
    assert not found, found[:10]


def test_clinic_pick_card_wired():
    """Подтверждение клиники (ADR 0010, CONTRACTS §8.1): тексты карточки и ошибок 409/422 есть в ui.yaml, chat.js
    рисует clinic_choices и шлёт POST /api/requests/{id}/clinic {contract_id, choice}, «none» — как в constants,
    демо-API отвечает на этот маршрут."""
    ui = _ui()
    for key in ("title", "asked", "hint", "empty", "none", "source", "busy", "chip_selected", "chip_none", "stale"):
        assert isinstance(ui["clinic_pick"].get(key), str), f"clinic_pick.{key}"
    for key in ("clinic_no_choice", "clinic_row_unknown"):
        assert isinstance(ui["errors"].get(key), str), f"errors.{key}"
    code = _strip_js_comments((STATIC / "chat.js").read_text(encoding="utf-8"))
    assert "clinic_choices" in code and '"/clinic"' in code
    assert re.search(r"contract_id:\s*contractId,\s*choice:\s*choice", code)
    assert f'CLINIC_PICK_NONE = "{K.CLINIC_PICK_NONE}"' in code
    assert '"no_clinic_choice"' in code and '"clinic_row_unknown"' in code
    mock = (STATIC / "mock_api.js").read_text(encoding="utf-8")
    assert "\\/clinic$/.exec(p)" in mock and "clinic_choices" in mock


def test_clinic_find_wired():
    """Поиск по перечню программы в карточке «Уточните клинику» (круг w7, CONTRACTS §8: GET …/clinic-candidates):
    тексты поиска и ошибок 409/422 — в ui.yaml (clinic_pick.find_*), у поля есть label и type=search, счётчик —
    aria-live, запрос с contract_id/q/offset, задержка ввода и отбрасывание устаревших ответов, «Показать ещё»
    переводит фокус на первую новую строку, выбор найденной строки — тем же pickClinic; демо-API отвечает на маршрут."""
    ui = _ui()
    for key in ("find_region", "find_label", "find_btn", "find_all", "find_results", "find_loading", "find_count",
                "find_more", "find_none", "find_none_all", "find_err_no_choice", "find_err_contract", "find_err"):
        val = ui["clinic_pick"].get(key)
        assert isinstance(val, str) and CYR.search(val), f"clinic_pick.{key}"
    assert "{shown}" in ui["clinic_pick"]["find_count"] and "{total}" in ui["clinic_pick"]["find_count"]
    assert "{q}" in ui["clinic_pick"]["find_none"]
    code = _strip_js_comments((STATIC / "chat.js").read_text(encoding="utf-8"))
    assert '"/clinic-candidates?contract_id="' in code and '"&q="' in code and '"&offset="' in code
    assert 'type: "search"' in code and 'el("label", { for: fid' in code
    assert re.search(r'class: "pick__count"[^}]*"aria-live": "polite"', code)
    assert "CLINIC_FIND_DELAY = 300" in code and "setTimeout(function () { run(q, true); }, CLINIC_FIND_DELAY)" in code
    assert "my !== st.seq" in code                       # последний запрос побеждает
    assert "paint(reset ? undefined : from)" in code and "btn.focus()" in code
    # Найденная строка выбирается тем же обработчиком, что и кандидат (одна функция строки).
    assert code.count("function clinicOptRow(") == 1 and "pickClinic(ch.contract_id, r.id, box)" in code
    assert code.count("clinicOptRow(ch, r, box") == 3     # определение + кандидаты + результаты поиска
    assert '"no_clinic_choice"' in code and '"contract_not_in_context"' in code
    for key in re.findall(r'T\("(clinic_pick\.find_\w+)"', code):
        assert isinstance(_lookup(ui, key), str), key
    mock = (STATIC / "mock_api.js").read_text(encoding="utf-8")
    assert "\\/clinic-candidates$/.exec(p)" in mock and '"contract_not_in_context"' in mock
    assert f"CLINIC_SEARCH_PAGE = {K.CLINIC_SEARCH_PAGE}" in mock
