"""Ревизия w8 (этап w8-fix): договор неизвестной СК в чате, запасной разборщик txt/pdf/xlsx, битый файл, снимок
обработанных файлов для reparse_all, сохранение договора при импорте."""
from __future__ import annotations

from dms_core import engine, onboarding
from dms_core.constants import Label
from dms_core.schemas import ChatContext
from fixtures import client_kit as ck

CLAUSE = "Страховщик организует и оплачивает экстренную госпитализацию застрахованного в стационар."


def _client(tmp, name="Клиент Г", legal="ООО Генерик"):
    reg = ck.registry(tmp)
    g = reg.create_client_group(name)
    cc = reg.add_client_contract(g.id, legal)
    return reg, g, cc


def _generic_client(tmp):
    reg, g, cc = _client(tmp)
    doc = ck.generic_docx(tmp / "src" / "Договор 5.docx", ["ДОГОВОР № 5 страхования", "1. Предмет договора",
                                                           f"1.1. {CLAUSE}", "2. Срок действия договора",
                                                           "2.1. Договор действует один год."])
    ck.put_file(reg, ck.clients_root(tmp), g.id, cc.id, doc)
    return reg, g, cc


def test_generic_contract_answers_by_clause_search_without_program_question():
    """Ноль программ (generic): программа не спрашивается, ответ — найденный пункт с проверенной ссылкой, метка
    без «можно» / «нельзя» (данных о программе нет)."""
    tmp = ck.workdir()
    reg, _g, cc = _generic_client(tmp)
    st = ck.kit_store(tmp)
    rep = onboarding.process(cc.id, store=st, registry=reg, root=ck.clients_root(tmp), run_selfcheck=False)
    assert rep.readiness == "limited" and rep.counts["programs"] == 0
    for q in (CLAUSE, f"Входит ли {CLAUSE[:-1].lower()}?"):
        a = engine.answer(q, ChatContext(group_id="ug1", contract_id=cc.id), llm=None, store=st)
        assert not any("программа у застрахованного" in x for x in a.questions), a.questions
        assert a.label in (Label.insufficient, None), a.label
        statements = list(a.statements) + [s for pc in a.per_contract for s in pc.statements]
        cited = [c for s in statements for c in s.citations if c.verified]
        assert cited and any("экстренную госпитализацию" in c.quote for c in cited)


def test_generic_question_words_not_required_in_clause():
    """Слова вопроса («входит ли») в пункте не стоят: поиск по основам слов услуги — пункт найден со ссылкой
    (проверка автора 25.10.2026: «Входит ли стоматологическая помощь?» получал пустой ответ)."""
    tmp = ck.workdir()
    reg, g, cc = _client(tmp)
    doc = ck.generic_docx(tmp / "src" / "Договор 7.docx", [
        "ДОГОВОР № 7 страхования", "1. Объём помощи",
        ("1.1. Программа включает амбулаторно-поликлиническую помощь, в том числе приём терапевта и "
         "стоматологическую помощь."), "1.2. Не оплачиваются услуги косметологии и пластической хирургии."])
    ck.put_file(reg, ck.clients_root(tmp), g.id, cc.id, doc)
    st = ck.kit_store(tmp)
    onboarding.process(cc.id, store=st, registry=reg, root=ck.clients_root(tmp), run_selfcheck=False)
    a = engine.answer("Входит ли стоматологическая помощь?", ChatContext(group_id=g.id, contract_id=cc.id),
                      llm=None, store=st)
    statements = list(a.statements) + [s for pc in a.per_contract for s in pc.statements]
    assert any("стоматологическую помощь" in c.quote for s in statements for c in s.citations if c.verified)


# ------------------------------------------------------------------ п. 2: запасной разборщик txt / pdf / xlsx

def test_generic_paragraphs_numbers_from_line_start():
    from dms_core.adapters.generic import paragraphs, split_number

    assert split_number("2.6.1. Текст пункта") == ("2.6.1", "Текст пункта")
    assert split_number("3 Срок") == ("3", "Срок")
    assert split_number("01.01.2026 договор вступает в силу")[0] is None      # дата — не номер пункта
    lines = ["ДОГОВОР № 7", "", "1. Предмет договора", "1.1. Страховщик оплачивает", "медицинские услуги.",
             "Второй абзац без номера."]
    assert list(paragraphs(lines)) == [(None, "ДОГОВОР № 7"), ("1", "Предмет договора"),
                                       ("1.1", "Страховщик оплачивает медицинские услуги."),
                                       (None, "Второй абзац без номера.")]


def test_generic_reads_txt_pdf_xlsx():
    tmp = ck.workdir()
    reg, g, cc = _client(tmp)
    root = ck.clients_root(tmp)
    txt = tmp / "src" / "Договор 8.txt"
    txt.parent.mkdir(parents=True, exist_ok=True)
    txt.write_text(f"ДОГОВОР № 8\n\n1. Предмет\n1.1. {CLAUSE}\n", encoding="cp1251")
    ck.put_file(reg, root, g.id, cc.id, txt, doc_type="contract")
    ck.put_file(reg, root, g.id, cc.id, ck.simple_pdf(tmp / "src" / "Memo.pdf", "2.1. Emergency care is covered."),
                doc_type="memo")
    ck.put_file(reg, root, g.id, cc.id, ck.simple_xlsx(tmp / "src" / "Таблица.xlsx",
                                                       [["Вид помощи", "Лимит"], ["Стоматология", "50 000"], []]),
                doc_type="other")
    st = ck.kit_store(tmp)
    rep = onboarding.process(cc.id, store=st, registry=reg, root=root, run_selfcheck=False)
    assert rep.readiness == "limited" and rep.adapter == "generic"
    by_doc: dict[str, list] = {}
    for cl in st.clauses(cc.id):
        by_doc.setdefault(cl.doc_id, []).append(cl)
    assert [(c.number, c.text) for c in by_doc["u01.contract"]] == [(None, "ДОГОВОР № 8"), ("1", "Предмет"),
                                                                    ("1.1", CLAUSE)]
    assert [(c.number, c.text, c.page) for c in by_doc["u01.memo"]] == [("2.1", "Emergency care is covered.", 1)]
    assert [c.text for c in by_doc["u01.other"]] == ["Вид помощи | Лимит", "Стоматология | 50 000"]
    # договор — checked (карточка generic без номера и программ), остальные документы — ready
    assert {d["title"]: d["status"] for d in rep.documents} == {"Договор 8": "checked", "Memo": "ready",
                                                                "Таблица": "ready"}


def test_generic_pdf_without_text_and_failed_report_has_parser_warnings():
    """PDF без текста — у файла «PDF не разбирается: загрузите DOCX»; договор без пунктов — failed, в отчёте —
    предупреждения разборщика (формат СК не распознан) и причина у документа."""
    tmp = ck.workdir()
    reg, g, cc = _client(tmp)
    root = ck.clients_root(tmp)
    ck.put_file(reg, root, g.id, cc.id, ck.simple_pdf(tmp / "src" / "Договор скан.pdf", None), doc_type="contract")
    rep = onboarding.process(cc.id, store=ck.kit_store(tmp), registry=reg, root=root, run_selfcheck=False)
    assert rep.readiness == "failed"
    assert any("формат СК не распознан" in w for w in rep.warnings)
    (d,) = rep.documents
    assert d["status"] == "error" and d["warnings"] == ["PDF не разбирается: загрузите DOCX."]


# ------------------------------------------------------------------ п. 5: битый файл не роняет договор

def test_broken_file_generic_is_error_only_for_that_file():
    tmp = ck.workdir()
    reg, g, cc = _generic_client(tmp)
    root = ck.clients_root(tmp)
    ck.put_bytes(reg, root, g.id, cc.id, "Памятка.docx", b"PK\x03\x04 broken", doc_type="memo")
    rep = onboarding.process(cc.id, store=ck.kit_store(tmp), registry=reg, root=root, run_selfcheck=False)
    assert rep.readiness == "limited" and rep.counts["clauses"] == 5
    st = {d["title"]: d for d in rep.documents}
    assert st["Памятка"]["status"] == "error" and "не читается" in st["Памятка"]["warnings"][0]
    assert st["Договор 5"]["status"] == "checked"                 # карточка generic: нет номера и программ


def test_broken_file_known_insurer_reparsed_without_it():
    """Адаптер СК разбирает договор целиком: битый файл ломает разбор — его находит probe_file, разбор повторяется
    без него; в отчёте ошибка у этого файла, готовность — по остальным."""
    from dms_core import adapters
    from helpers import patched

    tmp = ck.workdir()
    reg, g, cc = _client(tmp)
    root = ck.clients_root(tmp)
    for f in ck.ren_files(tmp):
        ck.put_file(reg, root, g.id, cc.id, f)
    ck.put_bytes(reg, root, g.id, cc.id, "Дополнительное соглашение 1.docx", b"not a zip", doc_type="amendment")
    real = adapters.parse_contract

    def strict(kc, r):                    # адаптер, которому нужны все файлы вместе: битый файл — исключение
        for f in kc.files:
            if onboarding.probe_file(r / f.path, f.fmt):
                raise ValueError("битый файл")
        return real(kc, r)
    with patched(adapters, "parse_contract", strict):
        rep = onboarding.process(cc.id, store=ck.kit_store(tmp), registry=reg, root=root, run_selfcheck=False)
    assert rep.readiness == "ready" and rep.adapter == "renaissance"
    bad = [d for d in rep.documents if d["status"] == "error"]
    assert len(bad) == 1 and "Дополнительное соглашение 1.docx" in bad[0]["warnings"][0]
    assert any("без нечитаемых файлов" in w for w in rep.warnings)

    def always(kc, r):                    # сбой в связке документов: без битого файла тоже не разобрать — failed
        raise ValueError("связка")
    with patched(adapters, "parse_contract", always):
        rep = onboarding.process(cc.id, store=ck.kit_store(tmp), registry=reg, root=root, run_selfcheck=False)
    assert rep.readiness == "failed" and any("Дополнительное соглашение 1.docx" in w for w in rep.warnings)


# ------------------------------------------------------------------ п. 8: id в предупреждениях

def _has_id(w: str) -> bool:
    import re

    return bool(re.search(r"\b(?:ug\d+|[cu]\d{2,})\.[a-z_]+", w))


def test_humanize_ids_and_group_docs_in_report():
    titles = {"u01.program": "Программа ДМС", "u01.contract": "Договор № 9"}
    numbers = {"u01.program#p12": "1.4"}
    assert onboarding.humanize("u01.program#p12: строка без названия", titles, numbers) == \
        "«Программа ДМС» → п. 1.4: строка без названия"
    assert onboarding.humanize("u01: Σ премий ≠ взносов; u01.contract", titles, numbers) == \
        "договор: Σ премий ≠ взносов; «Договор № 9»"
    assert onboarding.humanize("ug1/u01/Файл.docx: тип other", titles, numbers) == "Файл.docx: тип other"
    tmp = ck.workdir()
    reg, g, cc = _generic_client(tmp)
    rules = ck.generic_docx(tmp / "x" / "Правила страхования.docx", ["1. Общие положения"])
    ck.put_file(reg, ck.clients_root(tmp), g.id, cc.id, rules, scope="group", doc_type="rules")
    rep = onboarding.process(cc.id, store=ck.kit_store(tmp), registry=reg, root=ck.clients_root(tmp),
                             run_selfcheck=False)
    assert {(d["title"], d["scope"]) for d in rep.documents} == {("Договор 5", "contract"),
                                                                 ("Правила страхования", "group")}
    assert not any(_has_id(w) for d in rep.documents for w in d["warnings"])
    assert not any(_has_id(w) for w in rep.warnings)


# ------------------------------------------------------------------ пп. 6–7: снимок обработанных файлов

def test_reparse_uses_processed_snapshot_not_new_uploads():
    tmp = ck.workdir()
    reg, g, cc = _generic_client(tmp)
    root, st = ck.clients_root(tmp), ck.kit_store(tmp)
    assert onboarding.reparse_all(st, reg, root=root) == {cc.id: "not_processed"}   # не обрабатывался — пропуск
    onboarding.process(cc.id, store=st, registry=reg, root=root, run_selfcheck=False)
    snap = reg.processed_files(cc.id)
    assert [f.name for f in snap] == ["Договор 5.docx"]
    assert not onboarding.files_changed(reg.client_files(cc.id), snap)
    extra = ck.generic_docx(tmp / "src" / "Памятка.docx", ["1. Новая памятка не обработана."])
    ck.put_file(reg, root, g.id, cc.id, extra, doc_type="memo")
    assert onboarding.files_changed(reg.client_files(cc.id), snap)
    assert onboarding.reparse_all(st, reg, root=root) == {cc.id: "limited"}
    assert {d.id for d in st.documents(cc.id)} == {"u01.contract"}                   # новая загрузка не подхвачена


def test_reparse_keeps_contract_when_files_deleted_in_place_and_from_previous_db():
    from dms_core.store import KitStore

    tmp = ck.workdir()
    reg, g, cc = _generic_client(tmp)
    root, st = ck.clients_root(tmp), ck.kit_store(tmp)
    onboarding.process(cc.id, store=st, registry=reg, root=root, run_selfcheck=False)
    n, rev = len(st.clauses(cc.id)), st.revision(cc.id)
    (root / g.id / cc.id / "Договор 5.docx").unlink()          # файл удалён после обработки (своя временная папка)
    reg.delete_client_file(cc.id, "Договор 5.docx")
    # импорт в работающую базу (HTTP): договор на месте и не изменён
    assert onboarding.reparse_all(st, reg, root=root) == {cc.id: "kept"}
    assert st.revision(cc.id) == rev and len(st.clauses(cc.id)) == n
    # сборка новой базы (скрипт): договор переносится из прежней без изменений
    fresh = KitStore(tmp / "derived" / "new.sqlite")
    assert onboarding.reparse_all(fresh, reg, root=root, previous=st) == {cc.id: "kept"}
    assert [c.text for c in fresh.clauses(cc.id)] == [c.text for c in st.clauses(cc.id)]
    assert [d.status for d in fresh.documents(cc.id)] == [d.status for d in st.documents(cc.id)]
    assert {x.id: x.name for x in fresh.groups()}[g.id] == "Клиент Г"
    # без прежней базы договор без файлов — no_files (ничего не выдумывается)
    assert onboarding.reparse_all(KitStore(":memory:"), reg, root=root) == {cc.id: "no_files"}
