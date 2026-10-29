"""Ответ по документу, прикреплённому к чату (ADR 0016, dms_core.attachment): разбор, разделы, поиск, цитаты."""
from __future__ import annotations

import tempfile
from pathlib import Path

import helpers  # noqa: F401 — пути src/ и tests/
from dms_core import attachment as att
from dms_core.citations import locate_quote
from dms_core.constants import Confidence, DocType
from dms_core.schemas import Document

TXT = """1. Амбулаторно-поликлиническая помощь
1.1. Приём врачей-специалистов: терапевта, кардиолога, оториноларинголога по направлению.
1.2. Лабораторные исследования крови и мочи по назначению врача поликлиники.
2. Стоматологическая помощь
2.1. Лечение кариеса с применением светоотверждаемых пломб, удаление зубов под местной анестезией.
2.2. Профессиональная гигиена полости рта не входит в программу страхования.
3. Исключения
3.1. Не оплачиваются услуги иглорефлексотерапии, мануальной терапии и косметологии.
"""


def _doc(tmp: Path) -> tuple[Document, list]:
    p = tmp / "prog.txt"
    p.write_text(TXT, encoding="utf-8")
    import datetime as dt

    clauses = att.parse_file(p, "txt", "Программа", "att_test")
    doc = Document(id="att_test", title="Программа", doc_type=DocType.program, source_path="x",
                   original_format="txt", sha256="0" * 64, uploaded_at=dt.datetime.now(dt.UTC))
    return doc, clauses


def test_parse_numbers_and_sections():
    _, cl = _doc(Path(tempfile.mkdtemp()))
    assert [c.number for c in cl][:3] == ["1", "1.1", "1.2"]
    assert all(c.doc_id == "att_test" and c.contract_id is None for c in cl)
    names = [s[0] for s in att.sections(cl)]
    assert names == ["Амбулаторно-поликлиническая помощь", "Стоматологическая помощь", "Исключения"]


def test_intro_counts_sections_and_options_without_label():
    doc, cl = _doc(Path(tempfile.mkdtemp()))
    a = att.intro(doc, cl)
    assert a.label is None and a.reply is not None
    assert "загружен" in a.reply.lead and "8 пунктов" in a.reply.lead and "Стоматологическая помощь" in a.reply.lead
    assert len(a.reply.options) == 3 and a.reply.options[0].startswith("Что сказано в разделе")
    # ссылки на заголовки разделов — проверенные цитаты вложения
    cites = a.statements[0].citations
    assert cites and all(c.verified and c.doc_id == "att_test" for c in cites)


def test_answer_finds_clause_with_verified_quote():
    doc, cl = _doc(Path(tempfile.mkdtemp()))
    a = att.answer("Входит ли лечение кариеса?", doc, cl)
    assert a.label is None and a.confidence == Confidence.confirmed
    assert a.reply.points and a.reply.points[0].refs == [cl[4].id]
    assert "кариеса" in a.reply.points[0].quote
    assert "только по прикреплённому документу" in a.reply.caution
    for st in a.statements:
        for c in st.citations:
            text = next(x.text for x in cl if x.id == c.ref)
            assert c.verified and locate_quote(c.quote, text) is not None


def test_answer_not_found_has_no_points():
    doc, cl = _doc(Path(tempfile.mkdtemp()))
    a = att.answer("Сколько стоит полис для собаки?", doc, cl)
    assert not a.reply.points and "не нашлось" in a.reply.lead and a.confidence == Confidence.insufficient


def test_section_question_returns_section_clauses():
    doc, cl = _doc(Path(tempfile.mkdtemp()))
    a = att.answer("Что сказано в разделе «Исключения»?", doc, cl)
    assert [p.refs[0] for p in a.reply.points] == [cl[-1].id]


def test_rebind_to_contract_keeps_text():
    _, cl = _doc(Path(tempfile.mkdtemp()))
    out = att.rebind(cl, "c01.chat_ab", "c01")
    assert all(c.doc_id == "c01.chat_ab" and c.contract_id == "c01" and c.id.startswith("c01.chat_ab#")
               for c in out)
    assert [c.text for c in out] == [c.text for c in cl]


def test_plural():
    assert [att.plural(n, ["пункт", "пункта", "пунктов"]) for n in (1, 2, 5, 11, 21, 104)] == \
        ["пункт", "пункта", "пунктов", "пунктов", "пункт", "пункта"]


def test_search_keeps_only_clauses_about_the_service():
    """«Как удалить зуб» — пункт об удалении, без «чистки зубов» (ревизия точности w18)."""
    from dms_core.attachment import search
    from dms_core.schemas import Clause

    cls = [Clause(id=f"att#p{i}", doc_id="att", contract_id=None, number=n, path=f"Памятка › {n}", text=t)
           for i, (n, t) in enumerate([("2.1", "Удаление зуба проводится по направлению врача-стоматолога."),
                                       ("2.2", "Профессиональная чистка зубов один раз в год.")])]
    got = search("как удалить зуб?", cls, "test-focus")
    assert [c.number for c in got] == ["2.1"]
