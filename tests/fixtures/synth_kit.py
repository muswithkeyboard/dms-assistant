"""Синтетические ParsedContract для тестов хранилища, поиска и инструментов (без документов организаторов).

Ловушка ADR 0005: два договора одной ГК почти одинаковы (общие пункты совпадают дословно), у обоих программа
«Стандарт», но разная франшиза, разная стоматология и разные клиники. Ни одна функция не должна вернуть строку
чужого договора.
"""
from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from dms_core.adapters.base import ParsedContract, text_hash
from dms_core.constants import DocType
from dms_core.schemas import (
    CareMatrix,
    Clause,
    ClinicRow,
    Contract,
    Document,
    Franchise,
    Group,
    Payment,
    Program,
)

NOW = dt.datetime(2026, 10, 23, tzinfo=dt.UTC)

# Общие для обоих договоров пункты (дословно одинаковые — проверка дедупа)
COMMON = [
    ("1.1", "Страховщик обязуется организовать и оплатить медицинские услуги застрахованным лицам."),
    ("1.2", "Срок действия договора составляет один год с даты вступления в силу."),
    ("2.1", "Застрахованное лицо обращается в медицинскую организацию из перечня по программе."),
    ("2.2", "Экстренная госпитализация осуществляется по направлению скорой медицинской помощи."),
]

# Различия: стоматология и франшиза
VARIANTS = {
    "c01": {
        "dental": "Профессиональная гигиена полости рта: снятие зубного налёта методом AIR FLOW один раз в год.",
        "franchise": 10.0,
        "clinic": ("Клиника «Улыбка»", "ООО «Улыбка-Дент»", "г. Москва, ул. Тверская, д. 1", "Москва"),
        "premium": 50_000.0,
    },
    "c02": {
        "dental": "Стоматологическая помощь: лечение кариеса, удаление зубов. Профессиональная гигиена не входит.",
        "franchise": "—",
        "clinic": ("Медси на Белорусской", "АО «Медси»", "г. Москва, ул. Грузинский вал, д. 19", "Москва"),
        "premium": 42_000.0,
    },
}


def make_parsed(cid: str, group_id: str = "gk9", extra: int = 0) -> ParsedContract:
    """Договор cid с документами contract/program/clinics; extra — сколько дополнительных пунктов добавить."""
    v = VARIANTS.get(cid, VARIANTS["c01"])
    docs = [
        Document(id=f"{cid}.{t}", contract_id=cid, group_id=group_id, title=f"{t} {cid}", doc_type=DocType(t),
                 source_path=f"synth/{cid}/{t}.docx", original_format="docx", sha256="0" * 64, uploaded_at=NOW)
        for t in ("contract", "program", "clinics")
    ]
    clauses: list[Clause] = []

    def add(doc: str, number: str | None, text: str, program: str | None = None, path: str = "") -> None:
        doc_id = f"{cid}.{doc}"
        clauses.append(Clause(id=f"{doc_id}#p{len(clauses)}", doc_id=doc_id, contract_id=cid, number=number,
                              path=path or f"{doc} › {number or ''}", program=program, text=text,
                              text_hash=text_hash(text)))

    for number, text in COMMON:
        add("contract", number, text)
    add("program", "3.1", v["dental"], program="Стандарт", path="Программа «Стандарт» › Стоматология › 3.1")
    add("program", "3.1", "Помощь на дому: вызов врача на дом в пределах МКАД.", program="Премиум",
        path="Программа «Премиум» › На дому › 3.1")
    add("program", "4.1", f"Франшиза по программе составляет {v['franchise']} процентов стоимости услуг.",
        program="Стандарт", path="Программа «Стандарт» › Франшиза › 4.1")
    words = ("анализ", "консультация", "рентген", "массаж", "вакцинация", "справка", "диспансеризация", "лимит")
    for i in range(extra):
        text = (f"Пункт {i}: {words[i % len(words)]} {words[(i * 3) % len(words)]} оказывается по назначению "
                f"врача в объёме программы, код услуги {i}.")
        add("program", f"9.{i}", text, program="Стандарт" if i % 2 else None)

    name, legal, address, city = v["clinic"]
    clinics = [
        ClinicRow(id=f"{cid}.clinics#t0r{i}", contract_id=cid, doc_id=f"{cid}.clinics", program="Стандарт",
                  care_type=care, clinic_name=name, legal_name=legal, address=address, city=city,
                  franchise_pct=v["franchise"], by_referral=(care == "Помощь на дому"), source_ref=f"t0:r{i}")
        for i, care in enumerate(["Амбулаторно-поликлиническая помощь", "Стоматологическая помощь",
                                  "Помощь на дому"])
    ]
    care = [
        CareMatrix(contract_id=cid, program="Стандарт", care_type="dental", present=True, source_refs=["t0:r1"]),
        CareMatrix(contract_id=cid, program="Стандарт", care_type="inpatient", present=cid == "c01",
                   source_refs=["program#p5"]),
    ]
    contract = Contract(
        id=cid, group_id=group_id, legal_entity=f"Компания {int(cid[1:])}", number=f"Д-{cid}",
        date=dt.date(2026, 1, 1), period_from=dt.date(2026, 1, 1), period_to=dt.date(2026, 12, 31), headcount=10,
        programs=[Program(name="Стандарт", premium_per_person=v["premium"], persons=10)],
        payments=[Payment(n=1, due=dt.date(2026, 1, 15), amount=250_000.0),
                  Payment(n=2, due=dt.date(2026, 7, 15), amount=250_000.0)],
        franchise=Franchise(declared=cid == "c01"),
    )
    return ParsedContract(contract=contract, documents=docs, clauses=clauses, clinics=clinics, care=care)


def make_group_docs(group_id: str = "gk9") -> tuple[Group, list[Document], list[Clause]]:
    """Общие правила СК группы (contract_id=None): содержат «чистку зубов», но в поиске договора не участвуют."""
    doc = Document(id=f"{group_id}.rules", contract_id=None, group_id=group_id, title="Правила", doc_type=DocType.rules,
                   source_path="synth/rules.docx", original_format="docx", sha256="1" * 64, uploaded_at=NOW)
    text = "Правила страхования: чистка зубов и снятие налёта AIR FLOW оплачиваются всегда."
    cl = Clause(id=f"{doc.id}#p0", doc_id=doc.id, contract_id=None, number="1.1", path="Правила › 1.1", text=text,
                text_hash=text_hash(text))
    return Group(id=group_id, name="ГК 9"), [doc], [cl]
