"""Интерфейс подключения клиента (этап w8-ui, ADR 0011, CONTRACTS §9): вкладка на странице «Документы»,
тексты clients.* в ui.yaml, скрипт clients.js ходит по маршрутам §9, демо-API отвечает на все маршруты §9
со стадиями задания и ошибками 409 job_running / 422 no_files, доступность (label, aria-live, клавиатура)."""
from __future__ import annotations

import re

from dms_core.config import load_yaml
from helpers import ROOT, client

STATIC = ROOT / "src" / "dms_api" / "static"
TPL = ROOT / "src" / "dms_api" / "templates"
STAGES = ["received", "extracted", "checked", "indexed", "selfcheck"]


def _ui() -> dict:
    return load_yaml("texts_ru/ui.yaml")


def _code(name: str) -> str:
    src = (STATIC / name).read_text(encoding="utf-8")
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.DOTALL)
    return re.sub(r"(^|[^:\"'\\])//[^\n]*", r"\1", src)


def test_documents_page_has_clients_tab():
    """Страница «Документы» отдаёт вкладки (role=tablist), панель подключения клиента, область объявлений
    aria-live, диалог подтверждения удаления и подключает clients.js."""
    r = client().get("/documents")
    assert r.status_code == 200
    html = r.text
    assert 'role="tablist"' in html and 'id="tab-clients"' in html and 'aria-controls="panel-clients"' in html
    assert 'id="panel-clients"' in html and 'role="tabpanel"' in html
    assert re.search(r'id="cl-live"[^>]*aria-live="polite"', html)
    assert 'id="cl-del-dlg"' in html and "<dialog" in html
    assert "/static/clients.js" in html
    assert client().get("/static/clients.js").status_code == 200
    ui = _ui()["clients"]
    assert ui["tab_clients"] in html and ui["new_client"] in html


def test_clients_texts_complete():
    """Тексты подключения: стадии ТЗ 7.1 по порядку, все состояния договора и отчёта, адаптеры, счётчики отчёта,
    коды ошибок §9, подстановки в шаблонах строк."""
    ui = _ui()["clients"]
    assert list(ui["stages"]) == STAGES + ["done"]
    assert [ui["stages"][s] for s in ui["stages"]] == [
        "Загружен", "Текст извлечён", "Полнота проверена", "Проиндексирован", "Самопроверка", "Готов"]
    assert set(ui["states"]) == {"new", "running", "ready", "limited", "failed", "error"}
    assert ui["states"]["limited"] == "Готов с ограничениями"
    assert set(ui["adapters"]) == {"zetta", "renaissance", "sogaz", "generic"}
    assert set(ui["counts"]) == {"clauses", "numbered", "programs", "clinics", "care_rows", "mapped_tables",
                                 "mapped_matrices"}
    assert set(ui["errors"]) >= {"job_running", "no_files"}
    assert "{cited}" in ui["selfcheck"] and "{asked}" in ui["selfcheck"]
    assert "{missing}" in ui["readiness_limited"] and "недостаточно данных" in ui["readiness_limited"]


def test_clients_js_uses_contract_routes():
    """clients.js вызывает маршруты CONTRACTS §9 и создаёт чат по договору; опрос задания раз в секунду
    с остановкой на done/error; чат — только при ready/limited."""
    code = _code("clients.js")
    for route in ('"GET", "/api/clients"', '"POST", "/api/clients"', '"/api/clients/" + enc(g.id) + "/contracts"',
                  '"/api/clients/contracts/" + enc(ownerCid) + "/files"', '"PATCH", fileUrl(f)', '"DELETE", fileUrl(d.f)',
                  '"/api/clients/contracts/" + enc(cid) + "/process"', '"/api/clients/jobs/" + enc(p.id)', '"POST", "/api/chats"'):
        assert route in code, route
    assert "POLL_MS = 1000" in code
    assert 'job.state === "done" || job.state === "error"' in code and "stopPoll(cid)" in code
    assert 'if (r === "ready" || r === "limited")' in code
    assert '"job_running"' in code
    assert 'STAGES = ["received", "extracted", "checked", "indexed", "selfcheck", "done"]' in code


def test_clients_js_accessibility():
    """Зона загрузки: настоящая кнопка выбора файлов (клавиатура) + перетаскивание; поля мастера с label;
    select типа документа с aria-label; вкладки — стрелки/Home/End; удаление — через диалог подтверждения."""
    code = _code("clients.js")
    assert 'id: "cl-pick-" + key' in code and "input.click()" in code
    assert '"drop"' in code and '"dragover"' in code
    assert 'el("label", { for: id, text: label })' in code
    assert '"aria-label": T("clients.doc_type_for"' in code
    assert "ArrowRight" in code and "Home" in code and "End" in code
    assert "showModal" in code and "askDelete(" in code
    assert 'multiple: true' in code


def test_mock_api_has_onboarding_routes():
    """Демо-API (?mock=1) отвечает на все маршруты §9: создание клиента и договора, файлы (POST/PATCH/DELETE),
    обработка (202, 409 job_running, 422 no_files), опрос задания со стадиями received → … → selfcheck → done,
    отчёт с readiness ready / limited / failed; подключённый договор попадает в /api/groups."""
    mock = (STATIC / "mock_api.js").read_text(encoding="utf-8")
    for frag in ('p === "/api/clients" && method === "GET"', 'p === "/api/clients" && method === "POST"',
                 r"\/api\/clients\/(ug\d+)\/contracts$/", r"\/api\/clients\/contracts\/(u\d+)\/files$/",
                 r"\/api\/clients\/contracts\/(u\d+)\/files\/([^/]+)$/", r"\/api\/clients\/contracts\/(u\d+)\/process$/",
                 r"\/api\/clients\/jobs\/([^/]+)$/", 'method === "PATCH"', 'method === "DELETE"'):
        assert frag in mock, frag
    assert '"job_running"' in mock and "err(409" in mock
    assert '"no_files"' in mock and "err(422" in mock
    assert f"ONB_STAGES = {STAGES}".replace("'", '"') in mock
    for r in ('"ready"', '"limited"', '"failed"'):
        assert r in mock
    assert "function syncGroups()" in mock and "syncGroups();" in mock


def test_clients_partial_template_local():
    """Шаблон вкладки — без inline-скриптов и внешних адресов (проверяется общим тестом для всех *.html)."""
    src = (TPL / "_clients.html").read_text(encoding="utf-8")
    assert "<script" not in src and "http" not in src
