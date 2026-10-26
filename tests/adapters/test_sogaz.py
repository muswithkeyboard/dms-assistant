"""Адаптер «СОГАЗ» (ГК 3, c09–c13). Модульные тесты — без комплекта; сверка с комплектом и с прототипом
reference/kit_analysis/gk3/xl_data.json — SKIP, если данных нет."""
from __future__ import annotations

import json
from collections import Counter, defaultdict
from functools import cache

from helpers import ROOT, kit_or_skip, skip

GK3 = ("c09", "c10", "c11", "c12", "c13")
SHEET = "Список медицинских организаций"


# ------------------------------------------------------------------ без комплекта

def test_norm_number_auto_text_bullets_letters():
    from dms_core.adapters.sogaz import norm_number

    assert norm_number("1.3.", "Общая численность") == ("1.3", "auto")
    assert norm_number("IV.", "ИСКЛЮЧЕНИЯ") == ("IV", "auto")
    assert norm_number(None, "6.4.1. При необходимости") == ("6.4.1", "text")
    assert norm_number(None, "9.1.2 Индивидуальные Договоры") == ("9.1.2", "text")
    assert norm_number(None, "IV. ИСКЛЮЧЕНИЯ ИЗ ПРОГРАММЫ:") == ("IV", "text")
    assert norm_number("-", "текст") == (None, "none")
    assert norm_number("", "текст") == (None, "none")
    assert norm_number("а)", "Договор прекращается") == ("а)", "letter")
    assert norm_number(None, "Страхователь имеет право") == (None, "none")


def test_franchise_values_none_dash_number():
    from dms_core.adapters.sogaz import parse_franchise

    assert parse_franchise("0%") == 0.0 and parse_franchise("10 %") == 10.0
    assert parse_franchise("—") == "—" and parse_franchise("—") != 0
    assert parse_franchise(None) is None and parse_franchise("  ") is None


def test_missing_docs_from_annex_list():
    from dms_core.adapters.sogaz import find_missing_docs

    lines = ["Приложение 1. «Правила добровольного медицинского страхования» Страховщика",
             "Приложение 2. Программы добровольного медицинского страхования.",
             "Приложение 4. Список медицинских организаций.",
             "Приложения 7.1. – 7.6. Образцы заявлений",
             "Приложение 8. Порядок определения дополнительных медицинских организаций."]
    present = ["Договор № 9", "Приложение 2. Программы ДМС", "Приложение 4. Список медицинских организаций"]
    miss = find_missing_docs(lines, present)
    assert [m.split(".")[0] for m in miss] == ["Приложение 1", "Приложения 7", "Приложение 8"]


def _sheet(with_franchise: bool):
    hdr = [None, "Программа страхования", "Наименование ЛПУ", "Адрес ЛПУ", "Телефон", "Виды медицинской помощи"]
    if with_franchise:
        hdr.append("Безусловная франшиза, %")
    rows = [[None] * len(hdr) for _ in range(8)] + [hdr]
    rows.append([None, "Стандарт", "Клиника А", "101000, Москва г, Ленина ул, дом № 1", "+7 (495) 000-00-00",
                 "Амбулаторно-поликлиническая помощь\nДиспансеризация"] + (["10%"] if with_franchise else []))
    rows.append([None, "Стандарт", '"Организация услуг по направлению Страховщика"', "107078, Москва г",
                 "Организация услуг по направлению страховщика",
                 "АПО после Covid-19;\nПомощь на дому в пределах МКАД;\nСкорая медицинская помощь  в пределах МКАД;"]
                + (["—"] if with_franchise else []))
    rows.append([None, None, "Страховщик", None, "Страхователь", None] + ([None] if with_franchise else []))
    return {SHEET: rows}


def test_clinic_rows_split_care_types_and_pseudo_clinic():
    from dms_core.adapters.sogaz import parse_clinic_rows

    rows, warns, has_col = parse_clinic_rows(_sheet(True), doc_id="c10.clinics", contract_id="c10")
    assert has_col and not warns
    assert len(rows) == 5                                         # 2 вида + 3 вида; строка подписей пропущена
    assert {r.contract_id for r in rows} == {"c10"}
    a = [r for r in rows if r.clinic_name == "Клиника А"]
    assert [r.care_type for r in a] == ["Амбулаторно-поликлиническая помощь", "Диспансеризация"]
    assert a[0].source_ref == f"c10.clinics#{SHEET}!10" and a[0].franchise_pct == 10.0 and a[0].city == "Москва"
    assert len({r.id for r in rows}) == len(rows)
    pseudo = [r for r in rows if "направлению" in r.clinic_name]
    assert all(r.by_referral and r.franchise_pct == "—" for r in pseudo)
    assert "Скорая медицинская помощь в пределах МКАД" in [r.care_type for r in pseudo]   # «;» и двойной пробел
    assert "Помощь на дому в пределах МКАД" in pseudo[0].access_note


def test_clinic_rows_without_franchise_column_are_none():
    from dms_core.adapters.sogaz import parse_clinic_rows

    rows, _, has_col = parse_clinic_rows(_sheet(False), doc_id="c09.clinics", contract_id="c09")
    assert not has_col and all(r.franchise_pct is None for r in rows)


# ------------------------------------------------------------------ комплект

@cache
def _parsed(cid: str):
    from dms_core.adapters import parse_contract
    from dms_core.kit import scan

    root = kit_or_skip()
    kc = next(c for g in scan(root) for c in g.contracts if c.id == cid)
    return parse_contract(kc, root)


def _xl_data() -> dict:
    p = ROOT / "reference" / "kit_analysis" / "gk3" / "xl_data.json"
    if not p.is_file():
        skip(f"нет прототипа {p}")
    return json.loads(p.read_text(encoding="utf-8"))


def test_detect_sogaz_only_for_gk3():
    from dms_core.adapters import detect
    from dms_core.constants import Insurer
    from dms_core.kit import scan

    root = kit_or_skip()
    for g in scan(root):
        for kc in g.contracts:
            assert (detect(kc, root) is Insurer.sogaz) == (kc.id in GK3), kc.id


def test_contract_id_everywhere_and_unique_ids():
    for cid in GK3:
        r = _parsed(cid)
        assert r.contract.id == cid
        assert {x.contract_id for x in r.clauses} == {cid}
        assert {x.contract_id for x in r.clinics} == {cid}
        assert {x.contract_id for x in r.documents} == {cid}
        assert all(x.doc_id.startswith(cid + ".") for x in [*r.clauses, *r.clinics])
        ids = [x.id for x in r.clauses] + [x.id for x in r.clinics]
        assert len(ids) == len(set(ids)), cid


def test_card_headcount_programs_vip_only_c09():
    expect = {"c09": 183, "c10": 2, "c11": 44, "c12": 1, "c13": 1}
    progs = {"c09": ["Стандарт", "Бизнес", "ВИП"], "c10": ["Стандарт", "Бизнес"], "c11": ["Стандарт"],
             "c12": ["Бизнес"], "c13": ["Бизнес"]}
    for cid in GK3:
        c = _parsed(cid).contract
        assert c.headcount == expect[cid], cid
        assert [p.name for p in c.programs] == progs[cid], cid
        assert sum(p.persons for p in c.programs) == c.headcount
        assert c.period_from and c.period_to and len(c.payments) == 4
        has_vip = any(r.program == "ВИП" for r in _parsed(cid).clinics) or \
            any(x.program == "ВИП" for x in _parsed(cid).clauses)
        assert has_vip == (cid == "c09"), cid
    assert str(_parsed("c13").contract.period_from) == "2026-06-24"


def test_clinic_rows_match_prototype_xl_data():
    """Каждая строка xlsx с программой (без подписей) = группа ClinicRow с тем же source_ref; поля совпадают."""
    from dms_core.adapters.sogaz import _care_items, parse_franchise

    data = _xl_data()
    sizes = {"c09": 1421, "c10": 1024, "c11": 474, "c12": 550, "c13": 550}
    for cid in GK3:
        r = _parsed(cid)
        src = [row for row in data[str(int(cid[1:]))] if row[1]]
        assert len(src) == sizes[cid]
        by_ref = defaultdict(list)
        for x in r.clinics:
            by_ref[x.source_ref].append(x)
        assert len(by_ref) == len(src), cid
        for ref, row in zip(by_ref, src):          # порядок строк сохраняется
            got = by_ref[ref]
            assert got[0].program == row[1] and got[0].clinic_name == row[2] and got[0].address == row[3]
            assert [g.care_type for g in got] == _care_items(row[5])
            fr = parse_franchise(row[6]) if len(row) > 6 else None
            assert all(g.franchise_pct == fr for g in got)


def test_franchise_only_c10():
    data = _xl_data()
    c10 = _parsed("c10")
    by_ref = {x.source_ref: x.franchise_pct for x in c10.clinics}
    dist = Counter(by_ref.values())
    assert dist == {0.0: 715, 10.0: 82, 15.0: 38, 20.0: 43, "—": 146}
    assert dist["—"] == Counter(row[6] for row in data["10"] if row[1])["—"]
    assert not any("франшизы в документах не указан" in w for w in c10.warnings)
    for cid in ("c09", "c11", "c12", "c13"):
        r = _parsed(cid)
        assert r.contract.franchise.declared
        assert all(x.franchise_pct is None for x in r.clinics), cid
        assert any("размер франшизы в документах не указан" in w for w in r.warnings), cid


def test_footnotes_17_and_38_attached():
    for cid in GK3:
        cl = _parsed(cid).clauses
        c32 = [c for c in cl if c.doc_id == f"{cid}.program" and c.number == "3.2"]
        assert c32 and "30 км от МКАД" in c32[0].text
        assert any("возможности оказания данной услуги" in f for f in c32[0].footnotes), cid
        assert any(any("до 12:00" in f for f in c.footnotes) and c.path.endswith("6.1.3 (абзац)") for c in cl), cid


def test_missing_docs_and_two_variants_412():
    for cid in GK3:
        r = _parsed(cid)
        heads = [m.split(".")[0] for m in r.contract.missing_docs]
        assert heads == ["Приложение 1", "Приложение 3", "Приложение 5", "Приложения 7", "Приложение 8"], cid
        assert "Правила" in r.contract.missing_docs[0]
        cl = [c for c in r.clauses if c.doc_id == f"{cid}.contract"]
        i = next(k for k, c in enumerate(cl) if c.number == "4.1.2")
        tail = [c.text for c in cl[i + 1:i + 3]]
        assert "без ограничений" in tail[0] and "с учетом следующих условий" in tail[1]
        assert [c.number for c in cl[i + 3:i + 5]] == ["4.1.2.1", "4.1.2.2"]
        assert any("битая ссылка" in w for w in r.warnings), cid


def test_k17_both_fragments_available_c09():
    r = _parsed("c09")
    prog = [c for c in r.clauses if c.number == "3.2" and c.program == "Стандарт, Бизнес"]
    assert prog and "в пределах 30 км от МКАД" in prog[0].text
    std = [x for x in r.clinics if x.program == "Стандарт" and "направлению" in x.clinic_name]
    assert "Помощь на дому в пределах МКАД" in [x.care_type for x in std]
    vip = [x for x in r.clinics if x.program == "ВИП" and "направлению" in x.clinic_name]
    assert "Помощь на дому в пределах 30 км за МКАД" in [x.care_type for x in vip]
    assert any("Стандарт" in w and "различается" in w for w in r.warnings)
    assert not any("«ВИП» различается" in w for w in r.warnings)


def test_vip_numbering_continues_and_paths_disambiguate():
    cl = [c for c in _parsed("c09").clauses if c.doc_id == "c09.program"]
    vip = {c.number for c in cl if c.program == "ВИП"}
    assert "6.12.5.3" in vip                                   # как в Word/LibreOffice (gk3.md §2)
    p316 = [c for c in cl if c.number == "3.1.4.6"]
    assert {c.program for c in p316} == {"Стандарт, Бизнес", "ВИП"}
    assert len({c.path for c in p316}) == 2
