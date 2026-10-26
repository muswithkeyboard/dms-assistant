"""Экран разметки таблиц перечня клиник оператором (этап w9-ui, ADR 0012, CONTRACTS §10): clients.js ходит по
маршрутам §10, демо-API отвечает на них (таблицы, пробный разбор, сохранение/удаление разметки, профили СК),
поля insurer_name / force_generic в мастере и форме «Добавить юрлицо», роли колонок совпадают с ColumnRole,
тексты — в ui.yaml, select колонок подписаны «Колонка N: заголовок», итог проверки — в области aria-live."""
from __future__ import annotations

import re
import typing

from dms_core import schemas
from dms_core.config import load_yaml
from helpers import ROOT

STATIC = ROOT / "src" / "dms_api" / "static"
ROLES = list(typing.get_args(schemas.ColumnRole))


def _ui() -> dict:
    return load_yaml("texts_ru/ui.yaml")


def _code(name: str) -> str:
    src = (STATIC / name).read_text(encoding="utf-8")
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.DOTALL)
    return re.sub(r"(^|[^:\"'\\])//[^\n]*", r"\1", src)


def test_clients_js_uses_mapping_routes():
    """clients.js: GET …/tables, POST …/tables/try, PUT и DELETE …/tables/mapping (ref в запросе), профили СК;
    сохранение передаёт save_profile, insurer_name и process; ответ с заданием — опрос, как у «Обработать»."""
    code = _code("clients.js")
    assert 'return "/api/clients/contracts/" + enc(cid) + "/tables"' in code
    for frag in ('D.request("GET", tablesUrl(cid))', 'D.request("POST", tablesUrl(k) + "/try"',
                 'D.request("PUT", tablesUrl(k) + "/mapping"', 'D.request("DELETE", tablesUrl(k) + "/mapping" + q)',
                 '"?doc_id=" + enc(ref.doc_id) + "&kind=" + enc(ref.kind)', '"&index="', '"&sheet="',
                 'D.request("GET", "/api/insurer-profiles")', 'D.request("DELETE", "/api/insurer-profiles/" + enc(d.profile.id))'):
        assert frag in code, frag
    assert "save_profile: !!m.saveProfile" in code and "insurer_name: name || null" in code and "process: !!m.processNow" in code
    assert "startPoll(job.id, k)" in code
    # Кнопка разметки — в отчёте договора с readiness limited без перечня (или с перечнем из разметки).
    assert 'if ((r === "limited" && (!counts.clinics || byMapping)) || (r === "ready" && byMapping))' in code
    assert "var byMapping = counts.mapped_tables || counts.mapped_matrices;" in code


def test_clients_js_insurer_name_and_force_generic():
    """Мастер и «Добавить юрлицо» отправляют insurer_name и force_generic; флажок — с пояснением (aria-describedby)."""
    code = _code("clients.js")
    assert code.count("insurerExtra(") >= 3                      # объявление + мастер + форма юрлица
    assert 'insurer_name: $("cl-iname-" + n).value.trim() || null, force_generic: $("cl-fg-" + n).checked' in code
    assert 'insurer_name: $(p + "-iname").value.trim() || null' in code and 'force_generic: $(p + "-fg").checked' in code
    assert '"aria-describedby": fgId + "-hint"' in code


def test_column_roles_match_schema_and_texts():
    """Роли в выпадающем списке — ровно ColumnRole из schemas.py (в том же порядке), у каждой есть подпись;
    колонки пробного разбора — поля ClinicRow."""
    code = _code("clients.js")
    m = re.search(r"var ROLES = (\[[^\]]*\]);", code)
    # роль present — только у колонки матрицы (ADR 0013): в списке ролей перечня её нет
    list_roles = [r for r in ROLES if r != "present"]
    assert m and re.findall(r'"(\w+)"', m.group(1)) == list_roles
    ui = _ui()["clients"]["mapping"]
    assert set(ui["roles"]) == set(list_roles)
    mm = re.search(r"var MATRIX_ROLES = (\[[^\]]*\]);", code)
    matrix_roles = re.findall(r'"(\w+)"', mm.group(1)) if mm else []
    assert "present" in matrix_roles and set(matrix_roles) <= set(ROLES)
    assert set(ui["matrix_roles"]) == set(matrix_roles)
    t = re.search(r"var TRY_COLS = (\[[^\]]*\]);", code)
    cols = re.findall(r'"(\w+)"', t.group(1))
    assert cols == ["clinic_name", "address", "city", "care_type", "program", "franchise_pct"]
    assert set(cols) <= set(schemas.ClinicRow.model_fields) and set(ui["try_cols"]) == set(cols)


def test_mapping_accessibility():
    """Select каждой колонки подписан «Колонка N: заголовок» (aria-label), строка заголовка — радио с подписью,
    предпросмотр прокручивается внутри блока (role=region, tabindex), итог проверки — aria-live, ошибка у кнопок —
    role=alert; без колонки «Название клиники» запрос не уходит."""
    code = _code("clients.js")
    assert '"aria-label": colLabel(t, d.no_header ? null : d.header_row, i)' in code
    assert 'T("clients.mapping.col_label", { n: i + 1, head: head })' in code
    assert 'type: "radio", name: "cl-hdr-" + k' in code and '"aria-label": T("clients.mapping.header_pick"' in code
    assert 'class: "cl-map__grid", role: "region", tabindex: "0"' in code
    assert 'id: "cl-map-try-" + k, "aria-live": "polite"' in code
    assert 'id: errId, role: "alert"' in code
    assert 'if (!isMatrix(d)) { return hasRole(d, "clinic_name") ? null : T("clients.mapping.err_no_name"); }' in code
    assert code.count("de = draftError(t);") == 2 and code.count("if (de) { mapError(de, ") == 2
    ui = _ui()["clients"]
    assert "{n}" in ui["mapping"]["col_label"] and "{head}" in ui["mapping"]["col_label"]
    assert "Название клиники" in ui["mapping"]["err_no_name"]
    css = (STATIC / "app.css").read_text(encoding="utf-8")
    assert re.search(r"\.cl-map__grid \{[^}]*overflow-x: auto", css)


def test_mapping_texts_complete():
    """Тексты разметки и профилей: коды ошибок §10, подстановки, профиль «применён раз», «предложено»."""
    ui = _ui()["clients"]
    assert set(ui["errors"]) >= {"mapping_no_name", "mapping_bad_column", "table_not_found", "job_running"}
    mp = ui["mapping"]
    for key in ("open", "try", "save", "remove", "save_profile", "process_now", "const_program", "const_care",
                "suggested_chip", "state_saved", "state_profile", "try_total", "profile_name"):
        assert isinstance(mp[key], str) and mp[key], key
    assert "{name}" in mp["state_profile"] and "{n}" in mp["try_total"]
    assert set(ui["profiles"]["cols"]) == {"insurer", "signature", "used", "updated", "actions"}
    assert {"insurer_name", "force_generic", "force_generic_hint"} <= set(ui)


def test_mock_api_has_mapping_routes():
    """Демо-API: таблицы договора, пробный разбор (≤ MAPPING_SAMPLE_ROWS строк, total, warnings), PUT/DELETE разметки
    с ошибками mapping_no_name / mapping_bad_column / table_not_found / job_running, профили СК (GET, DELETE),
    insurer_name / force_generic у договора; обработка generic применяет разметку и профиль по подписи."""
    from dms_core import constants as K

    mock = (STATIC / "mock_api.js").read_text(encoding="utf-8")
    assert r"/^\/api\/clients\/contracts\/(u\d+)\/tables(\/try|\/mapping)?$/" in mock
    assert 'p === "/api/insurer-profiles" && method === "GET"' in mock
    assert r"/^\/api\/insurer-profiles\/(ip\d+)$/" in mock
    for code in ('"mapping_no_name"', '"mapping_bad_column"', '"table_not_found"', '"job_running"'):
        assert code in mock, code
    assert f"PREVIEW_ROWS = {K.TABLE_PREVIEW_ROWS}, SAMPLE_ROWS = {K.MAPPING_SAMPLE_ROWS}" in mock
    assert "rows: a.rows.slice(0, SAMPLE_ROWS), total: a.rows.length, suspicious: su" in mock
    assert 'kind: "docx_table"' in mock and 'kind: "xlsx_sheet"' in mock
    assert "insurer_name:" in mock and "force_generic: !!b.force_generic" in mock
    assert "applyMappings(j.contract_id, docs)" in mock and "prof.used += 1" in mock
    assert "mapped_tables: mapped.tables" in mock


def test_mapping_without_header_row_ui_and_mock():
    """Дополнение §10: «Строки заголовка нет» (header_row: null, радио выключены), «Программа — из названия
    таблицы» (program_from_title, поле программы выключено), «Применить ко всем таблицам с такой же раскладкой (N)»
    (apply_same_layout, N — same_layout из списка таблиц); подпись профиля по раскладке — словами. Демо-API: таблицы
    программ без заголовка, предложение по раскладке, apply_same_layout → applied_tables, профиль по раскладке."""
    code = _code("clients.js")
    assert "header_row: d.no_header ? null : d.header_row" in code and "program_from_title: !!d.from_title" in code
    assert "disabled: d.no_header" in code and 'byTitle ? T("clients.mapping.const_by_title")' in code
    assert "apply_same_layout: same" in code and 'T("clients.mapping.same_layout", { n: nSame })' in code
    assert "Number(t.same_layout) || 1" in code and "data.applied_tables" in code
    assert 'T("clients.profiles.layout_sig"' in code
    ui = _ui()["clients"]
    mp = ui["mapping"]
    for key in ("no_header", "no_header_hint", "from_title", "from_title_hint", "const_by_title", "same_layout",
                "same_layout_hint", "tables_n", "state_layout", "title_row", "head_title", "suggested_note_layout"):
        assert isinstance(mp[key], str) and mp[key], key
    assert "{n}" in mp["same_layout"] and "{n}" in mp["tables_n"] and "{cols}" in ui["profiles"]["layout_sig"]
    mock = (STATIC / "mock_api.js").read_text(encoding="utf-8")
    assert "function mLayout(rows)" in mock and "header_row: null, program_from_title: true" in mock
    assert "body.apply_same_layout === true" in mock and "applied_tables: applied" in mock
    assert "layout: mLayout(t.rows), same_layout: same" in mock


def test_mapping_fix_ui_and_mock():
    """w9-fix: таблица, которую профиль СК размечает сам (applied_profile), — «Размечено профилем СК «…»», не
    «предложено»; подозрительная разметка — причины и флажок подтверждения (confirm_suspicious в PUT); без источника
    программы — ошибка до запроса; похожие неразмеченные таблицы помечены; демо-API отвечает теми же кодами."""
    code = _code("clients.js")
    assert "t.applied_profile" in code and 'T("clients.mapping.state_profile_applied"' in code
    assert 'm.source === "applied"' in code and 'T("clients.mapping.applied_note_profile"' in code
    assert "confirm_suspicious: !!(m.suspicious && m.confirm)" in code and "function confirmBlock(c)" in code
    assert 'errCode(r) === "mapping_suspicious"' in code
    assert "hasProgramSource(m.draft)" in code and 'T("clients.mapping.err_no_program")' in code
    assert "t.similar_unmapped" in code and 'T("clients.mapping.state_similar")' in code
    mp = _ui()["clients"]["mapping"]
    for key in ("state_profile_applied", "state_similar", "state_suspicious", "applied_note_profile",
                "err_no_program", "confirm_suspicious", "confirm_suspicious_hint", "suspicious_title"):
        assert isinstance(mp[key], str) and mp[key], key
    assert "{name}" in mp["state_profile_applied"] and "«Вариант № N" in mp["from_title_hint"]
    assert {"mapping_no_program", "mapping_not_generic"} <= set(_ui()["clients"]["errors"])
    mock = (STATIC / "mock_api.js").read_text(encoding="utf-8")
    for x in ('"mapping_no_program"', '"mapping_suspicious"', "body.confirm_suspicious !== true",
              "applied_profile: auto ? auto.id : null", "function insurerKey(v)", "if (prof && !used[prof.id])"):
        assert x in mock, x


def test_matrix_mapping_ui_and_mock():
    """ADR 0013, CONTRACTS §11: у таблицы — «Это перечень клиник / Это матрица видов помощи»; матрица — «Строки —
    виды помощи, колонки — программы» (колонка видов помощи, над колонками — программа: текст заголовка по умолчанию
    или «не программа») или «Длинная таблица» (роли program / care_type / present); у перечня — флажок «Виды помощи
    из этого перечня → матрица» (derive_care). Пробный разбор матрицы — таблица программа × вид помощи «✓ входит» /
    «✗ не входит» и пропущенные ячейки. Демо-API: таблица-матрица, предложение по программам, коды mapping_no_care /
    mapping_no_program, try → matrix / skipped, отчёт — care_rows / mapped_matrices."""
    code = _code("clients.js")
    for frag in ('role: "care_matrix", matrix_layout: "wide", header_row: hr, columns: cols, program_cols: pc',
                 'role: "care_matrix", matrix_layout: "long"', 'derive_care: !!d.derive',
                 'T("clients.mapping.kind_matrix")', 'T("clients.mapping.layout_wide")', 'T("clients.mapping.layout_long")',
                 '"aria-label": T("clients.mapping.prog_name", { n: i + 1 })',
                 '"aria-label": T("clients.mapping.not_program_for", { n: i + 1 })',
                 'T("clients.mapping.err_no_care")', 'T("clients.mapping.err_no_program_matrix")',
                 'D.chip(T("clients.mapping.yes_mark"), "chip--ok")', 'D.chip(T("clients.mapping.no_mark"), "chip--danger chip--x")',
                 'T("clients.mapping.skipped_title"', 'T("clients.mapping.derive")',
                 'T("clients.mapping.try_matrix_derive"', 'T("clients.mapping.state_matrix_guess")'):
        assert frag in code, frag
    # программа колонки по умолчанию — текст заголовка; «не программа» колонку исключает
    assert "value: pg.name !== null ? pg.name : hdr, disabled: pg.skip" in code
    assert "if (i === d.care_col || (pg && pg.skip)) { continue; }" in code
    mp = _ui()["clients"]["mapping"]
    for key in ("kind_title", "kind_list", "kind_matrix", "layout_title", "layout_wide", "layout_long", "care_col",
                "not_program", "derive", "derive_hint", "state_matrix", "state_matrix_guess", "try_matrix_total",
                "try_matrix_caption", "yes_mark", "no_mark", "err_no_care", "err_no_program_matrix",
                "confirm_suspicious_matrix", "suggested_note_matrix"):
        assert isinstance(mp[key], str) and mp[key], key
    assert "{n}" in mp["prog_name"] and "{n}" in mp["skipped_title"] and set(mp["skipped_cols"]) == {"cell", "text"}
    assert {"mapping_no_care", "mapping_no_program"} <= set(_ui()["clients"]["errors"])
    css = (STATIC / "app.css").read_text(encoding="utf-8")
    assert re.search(r"\.cl-map__mgrid \{[^}]*overflow-x: auto", css)
    mock = (STATIC / "mock_api.js").read_text(encoding="utf-8")
    for x in ("function mRowsMatrix()", "function matrixGuess(rows, progs)", "function mApplyMatrix(cid, t, mp)",
              "function deriveCare(rows)", '"mapping_no_care"', "matrix: mx.matrix.slice(0, MATRIX_ROWS)",
              "mapped_matrices: mapped.matrices || 0"):
        assert x in mock, x
    from dms_core import constants as K
    assert f"MATRIX_ROWS = {K.MATRIX_SAMPLE_ROWS}" in mock


def test_all_programs_option_ui():
    """Дополнение CONTRACTS §11 (w10-fix): у «Программа для всей таблицы» (перечень и длинная матрица) — флажок «Все
    программы договора» (TableMapping.program = "*"), поле названия программы при этом выключено."""
    from dms_core import table_mapping as tm

    code = _code("clients.js")
    assert f'var ALL_PROGRAMS = "{tm.ALL_PROGRAMS}";' in code
    for frag in ('d.program = cb.checked ? ALL_PROGRAMS : ""', 'T("clients.mapping.const_program_all")',
                 'T("clients.mapping.const_program_all_hint")', "disabled: dis || all"):
        assert frag in code, frag
    mp = _ui()["clients"]["mapping"]
    assert mp["const_program_all"] and mp["const_program_all_hint"]
