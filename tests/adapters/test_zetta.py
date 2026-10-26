"""Адаптер Зетты (ГК 1, договоры c01–c04). Тесты на комплекте — SKIP без data/raw/; разбор T11 и чисел проверяется
и без комплекта на собственных синтетических строках."""
from __future__ import annotations

import csv
import functools
from pathlib import Path

from dms_core.io.docx_reader import Cell, Table
from helpers import ROOT, kit_or_skip, skip

GK1 = ("c01", "c02", "c03", "c04")
# Эталон числа строк ЛПУ в T11 по договорам — прототип reference/kit_analysis/gk1/t11_clinics.csv (3 758 строк)
PROTO_ROWS = {"c01": 744, "c02": 363, "c03": 2000, "c04": 651}
# Итог премии п. 2.4 (docs/facts/kit/gk1.md §1)
TOTALS = {"c01": 227_040.0, "c02": 2_317_500.0, "c03": 1_473_815.0, "c04": 9_288_380.0}


@functools.cache
def _parsed(cid: str):
    from dms_core.adapters import zetta
    from dms_core.kit import scan

    root = kit_or_skip()
    kc = next(c for g in scan(root) for c in g.contracts if c.id == cid)
    return zetta.parse(kc, root)


# ---------------------------------------------------------------- без комплекта

def _table(lines: list[str]) -> Table:
    return Table(index=11, rows=[[Cell(text=x)] for x in lines], para_before=0)


def test_parse_amount_three_formats():
    from dms_core.adapters.zetta import parse_amount

    assert parse_amount("56.760,00") == 56760.0
    assert parse_amount("227 040.00 (Двести двадцать семь тысяч сорок и 00/100)") == 227040.0
    assert parse_amount("19 340,00") == 19340.0
    assert parse_amount("2.322.095,00") == 2322095.0
    assert parse_amount("368.453,75") == 368453.75
    assert parse_amount("2000 рублей") == 2000.0
    assert parse_amount("нет") is None


def test_care_type_strips_franchise_prefix():
    from dms_core.adapters.zetta import care_type_of

    assert care_type_of('Франшиза (требуется активация) - 15%. программа "Стандарт". Вызов врача на дом.') == \
        ('Программа "Стандарт". Вызов врача на дом', 15.0)
    assert care_type_of('Программа "Стандарт". Вызов врача на дом') == ('Программа "Стандарт". Вызов врача на дом', None)
    assert care_type_of('Франшиза (требуется активация) - 10%, программа "Стандарт". Стационар.')[1] == 10.0


def test_t11_automaton_common_blocks_and_franchise():
    """«Общие для вариантов» в начале и в конце таблицы относятся к перечисленным вариантам, «Общие для всех» —
    ко всем; франшиза берётся из заголовка раздела над строкой ЛПУ."""
    from dms_core.adapters.zetta import parse_t11

    t = _table([
        "Виды медицинской помощи и Медицинские учреждения",
        "Общие для вариантов: 1 Город Стандарт, 3 Город Стандарт старше 60",
        "Страхование критических заболеваний",
        "Вариант № 1 Город Стандарт",
        'Программа "Стандарт". Поликлиника.',
        '1. ООО "Гиппократ" Адрес: 344019, Обл, Город г, ул, д.8',
        'Франшиза (требуется активация) - 15%. программа "Стандарт". Поликлиника.',
        '1. ООО "Гиппократ 21 век" Адрес: 344019, Обл, Город г, ул, д.8',
        "Вариант № 2 Город Стандарт родственник",
        'Программа "Стандарт". Поликлиника.',
        '1. ООО "Гиппократ" Адрес: 344019, Обл, Город г, ул, д.8',
        "Вариант № 3 Город Стандарт старше 60",
        "Общие для всех вариантов:",
        "Страхование от несчастного случая",
        "Общие программы для всех вариантов обслуживания:",
        "1. ЛПУ по выбору Страховщика (с франшизой в этом ЛПУ, если она предусмотрена программой страхования)",
    ])
    p = parse_t11(t)
    assert p.variants == {1: "Город Стандарт", 2: "Город Стандарт родственник", 3: "Город Стандарт старше 60"}
    by_row = {r.row: r for r in p.rows}
    assert by_row[2].kind == "section" and by_row[2].scope.variants == [1, 3]      # вариант 2 не входит
    assert by_row[13].scope.variants == [1, 2, 3]                                    # «для всех»
    assert by_row[5].kind == "item" and by_row[5].franchise_pct is None
    assert by_row[7].franchise_pct == 15.0 and by_row[7].name == 'ООО "Гиппократ 21 век"'
    assert by_row[7].care_type == by_row[5].care_type == 'Программа "Стандарт". Поликлиника'
    assert by_row[7].address == by_row[5].address                                    # один адрес — две строки
    assert by_row[15].kind == "item" and by_row[15].scope.variants == [1, 2, 3]


def test_text_number_split():
    from dms_core.adapters.zetta import split_number
    from dms_core.io.docx_reader import Para

    def para(text, number=None):
        return Para(index=0, number=number, level=None, text=text, style=None)

    assert split_number(para("2.8.1. Страховщик организовывает")) == ("2.8.1.", "Страховщик организовывает")
    assert split_number(para("4.3.2    По программам")) == ("4.3.2", "По программам")
    assert split_number(para("1.1б. Условия страхования")) == ("1.1б.", "Условия страхования")
    assert split_number(para("Текст", number="2.6.1.")) == ("2.6.1.", "Текст")
    assert split_number(para("01.01.2026 г. начало")) == (None, "01.01.2026 г. начало")   # дата — не номер
    assert split_number(para("пункт", number="­")) == (None, "пункт")              # маркер списка


# ---------------------------------------------------------------- на комплекте

def test_matches_only_gk1():
    from dms_core.adapters import zetta
    from dms_core.kit import scan

    root = kit_or_skip()
    found = {c.id for g in scan(root) for c in g.contracts if zetta.matches(c, root)}
    assert found == set(GK1)


def test_card_premium_sum_equals_total():
    from dms_core.adapters.zetta import total_premium

    for cid in GK1:
        p = _parsed(cid)
        c = p.contract
        s = sum(x.premium_per_person * x.persons for x in c.programs)
        assert total_premium(p) == TOTALS[cid], cid
        assert abs(s - TOTALS[cid]) < 0.01, (cid, s)
        assert abs(sum(x.amount for x in c.payments) - TOTALS[cid]) < 0.01, cid
        assert c.headcount == sum(x.persons for x in c.programs), cid
        assert c.number == cid[-1] and c.insurer.value == "zetta"
        assert str(c.date) == "2025-12-10" and str(c.period_from) == "2026-01-01" and str(c.period_to) == "2028-12-31"
        assert not p.warnings, (cid, p.warnings)


def test_payment_schedule_c01():
    """К10: четыре взноса по 56 760 руб. («56.760,00»), предельные даты 31.01 / 01.05 / 31.07 / 31.10.2026."""
    c = _parsed("c01").contract
    assert [(str(x.due), x.amount) for x in c.payments] == [
        ("2026-01-31", 56760.0), ("2026-05-01", 56760.0), ("2026-07-31", 56760.0), ("2026-10-31", 56760.0)]
    assert c.payments[3].period == "01.10.2026–31.12.2026"


def test_hippocrates_franchise_not_merged():
    """К14: два «Гиппократа» по одному адресу — 0 % и 15 % на поликлинику; у «Гиппократа 21 век» в стационаре 0 %."""
    rows = [r for r in _parsed("c01").clinics if r.program == "Ростов-на-Дону Стандарт ВЗР Ковид"]
    apo = 'Программа "Стандарт". Поликлиническое обслуживание без стоматологии, без вызова врача на дом'
    g = {r.clinic_name: r for r in rows if "Гиппократ" in r.clinic_name and r.care_type == apo}
    assert g['ООО "Медицинский Центр "Гиппократ"'].franchise_pct == "—"     # п. 2.11: не применяется, не 0 %
    assert g['ООО "МЦ "Гиппократ 21 век"'].franchise_pct == 15.0
    assert g['ООО "Медицинский Центр "Гиппократ"'].address == g['ООО "МЦ "Гиппократ 21 век"'].address
    home = [r for r in rows if "Гиппократ 21 век" in r.clinic_name and r.care_type.endswith("Вызов врача на дом")]
    assert [r.franchise_pct for r in home] == [15.0]
    medsi = [r for r in rows if "Медси" in r.clinic_name]
    assert sorted(r.franchise_pct for r in medsi) == [10.0, 10.0]
    hosp = [r for r in rows if "Гиппократ 21 век" in r.clinic_name and "Стационар" in r.care_type]
    assert [r.franchise_pct for r in hosp] == ["—"]
    assert all(r.source_ref.startswith("c01.contract#t11r") for r in rows)


def test_franchise_declared_with_mechanics():
    p = _parsed("c01")
    f = p.contract.franchise
    assert f.declared
    texts = {c.id: c for c in p.clauses}
    assert any(texts[i].number == "2.11." for i in f.mechanics_clause_ids)
    assert any("2000 рублей" in texts[i].text for i in f.mechanics_clause_ids)


def test_contract_id_everywhere():
    for cid in GK1:
        p = _parsed(cid)
        assert {r.contract_id for r in p.clinics} == {cid}
        assert {c.contract_id for c in p.clauses} == {cid}
        assert {m.contract_id for m in p.care} == {cid}
        assert all(r.doc_id == f"{cid}.contract" for r in p.clinics)
        assert len({c.id for c in p.clauses}) == len(p.clauses)
        assert len({r.id for r in p.clinics}) == len(p.clinics)
        names = {x.name for x in p.contract.programs}
        assert {r.program for r in p.clinics} == names, cid


def test_clinic_row_counts():
    for cid, n in PROTO_ROWS.items():
        assert len({r.source_ref for r in _parsed(cid).clinics}) == n, cid


def test_clinics_match_prototype_csv():
    """Построчная сверка с прототипом: строка T11, название, адрес, франшиза, заголовок раздела."""
    proto = ROOT / "reference" / "kit_analysis" / "gk1" / "t11_clinics.csv"
    if not proto.is_file():
        skip(f"нет прототипа {proto}")
    with Path(proto).open(encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    assert len(rows) == 3758
    for cid in GK1:
        mine = {}
        for r in _parsed(cid).clinics:
            mine.setdefault(int(r.source_ref.rsplit("r", 1)[1]), r)
        ref = [x for x in rows if x["doc"] == cid[-1]]
        assert len(ref) == len(mine) == PROTO_ROWS[cid]
        for x in ref:
            r = mine[int(x["row"])]
            assert (r.access_note if r.franchise_pct is None else r.clinic_name) == x["name"], (cid, x["row"])
            assert (r.address or "") == x["addr"], (cid, x["row"])
            # прототип: процент — если указан; пусто — заглушка ЛПУ (None) или «не применяется» («—», п. 2.11)
            if x["franchise"]:
                assert r.franchise_pct == float(x["franchise"]), (cid, x["row"])
            elif r.access_note is not None and r.access_note == x["name"]:
                assert r.franchise_pct is None, (cid, x["row"])          # «ЛПУ по выбору Страховщика» и т. п.
            else:
                assert r.franchise_pct == "—", (cid, x["row"])


def test_common_blocks_apply_to_listed_variants():
    """Д3: блок «Общие для вариантов: 1, 2, 3, 5, 6» (критические заболевания) — не для варианта 4 «родственник»;
    Д1: ВЗР — разные тексты для вариантов 1, 3, 4 и 2, 5."""
    p = _parsed("c03")
    crit = {m.program for m in p.care if m.care_type.startswith("Страхование критических заболеваний")}
    progs = [x.name for x in p.contract.programs]
    assert crit == set(progs) - {"Нижний Новгород Стандарт+АПО Персона+Балахна родственник"}
    p1 = _parsed("c01")
    travel = {}
    for m in p1.care:
        if m.care_type.startswith("Страхование выезжающих за рубеж"):
            travel.setdefault(m.care_type, set()).add(m.program)
    assert sorted(len(v) for v in travel.values()) == [2, 3]
    covid = {m.program for m in p1.care if "коронавирусной" in m.care_type}
    assert covid == {x.name for x in p1.contract.programs}


def test_clause_numbers_and_paths():
    p = _parsed("c01")
    nums = {c.number for c in p.clauses}
    assert {"2.6.1.", "2.8.", "2.8.1.", "2.8.4.", "2.11.", "4.3.2", "10.3.1."} <= nums
    c = next(c for c in p.clauses if c.number == "2.8.1.")
    assert c.text.startswith("Страховщик организовывает") and c.path.startswith("Договор › ЧИСЛЕННОСТЬ")
    sched = [c for c in p.clauses if "56.760,00" in c.text]
    assert len(sched) == 4 and all("таблица" in c.path for c in sched)
    prog = [c for c in p.clauses if c.program == "Рязань Стандарт" and c.path.startswith("Приложение 2")]
    assert prog and any("Стоматологическое обслуживание" in c.text for c in prog)
    assert any(c.path.startswith("Приложение 2 › ОБЪЕМ И ПОРЯДОК") for c in p.clauses)


def test_parse_is_idempotent():
    from dms_core.adapters import zetta
    from dms_core.kit import scan

    root = kit_or_skip()
    kc = next(c for g in scan(root) for c in g.contracts if c.id == "c02")
    a, b = zetta.parse(kc, root), zetta.parse(kc, root)
    assert [x.model_dump() for x in a.clauses] == [x.model_dump() for x in b.clauses]
    assert [x.model_dump() for x in a.clinics] == [x.model_dump() for x in b.clinics]
    assert a.contract == b.contract
