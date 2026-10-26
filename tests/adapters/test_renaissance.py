"""Адаптер «Ренессанс» (ГК 2): синтетический комплект (всегда) и сверка с комплектом организаторов (SKIP без
data/raw/). Эталон строк клиник — reference/kit_analysis/gk2/rows5–8.json (прототип clinics.py)."""
from __future__ import annotations

import json
import tempfile
from functools import cache
from pathlib import Path

from dms_core.adapters import renaissance as ren
from helpers import ROOT, kit_or_skip, skip

REF = ROOT / "reference" / "kit_analysis" / "gk2"


def _fixture():
    import sys

    sys.path.insert(0, str(ROOT / "tests" / "fixtures"))
    from ren_fixture import build

    tmp = Path(tempfile.mkdtemp(prefix="ren_"))
    kc, root = build(tmp)
    return kc, root


@cache
def _fixture_parsed():
    kc, root = _fixture()
    return kc, root, ren.parse(kc, root)


# ------------------------------------------------------------------ синтетический комплект

def test_fixture_matches_and_card():
    kc, root, pc = _fixture_parsed()
    assert ren.matches(kc, root)
    c = pc.contract
    assert (c.number, c.headcount, str(c.date), str(c.period_from), str(c.period_to)) == (
        "99", 3, "2026-04-24", "2026-04-25", "2027-04-24")
    assert [(p.name, p.premium_per_person, p.persons) for p in c.programs] == [("Альфа", 100.0, 2), ("Бета", 50.0, 1)]
    assert "не указана" in c.programs[1].notes              # пустая сумма лекарств — не 0
    assert [(x.n, str(x.due), x.amount) for x in c.payments] == [(1, "2026-05-25", 125.0), (2, "2026-11-25", 125.0)]
    assert sum(p.premium_per_person * p.persons for p in c.programs) == ren.premium_total(pc) == 250.0
    assert c.franchise.declared and pc.warnings == []


def test_fixture_contract_numbers_and_subitems():
    _, _, pc = _fixture_parsed()
    nums = {c.number: c for c in pc.clauses if c.doc_id == "c99.contract" and c.number}
    assert {"1.1", "2.3", "2.4", "2.7", "3.4.5", "4.1", "7.1"} <= set(nums)
    sub = nums["3.4.5, подп. 2"]
    assert "2 (двух) месяцев" in sub.text and sub.path == "Договор › 3.4.5 › 2)"
    # строка таблицы п. 2.3 — отдельный источник с программой
    row = next(c for c in pc.clauses if c.id == "c99.contract#t0r2")
    assert row.program == "Бета" and row.path == "Договор › 2.3 › Бета"


def test_fixture_clinics_fill_icons_notes_franchise():
    _, _, pc = _fixture_parsed()
    rows = {r.source_ref: r for r in pc.clinics}
    a1, a2 = rows["c99.program#t0r2"], rows["c99.program#t0r3"]
    assert a2.clinic_name == a1.clinic_name == 'Клиника А (ООО "Клиника А")'        # протяжка вниз
    assert a1.legal_name == 'ООО "Клиника А"' and a1.city == "Химки" and a2.city == "Москва"
    assert a1.care_type == "Поликлиника | Врач на дом" and a1.by_referral
    assert "по записи через центр медицинского обслуживания" in a1.access_note
    assert "только взрослое отделение" in a2.access_note and "только взрослое" not in a1.access_note
    z = rows["c99.program#t0r6"]
    assert z.franchise_pct == 10.0 and not z.by_referral and z.code == "С1_фр"
    assert all(r.contract_id == "c99" and r.doc_id == "c99.program" for r in pc.clinics)


def test_fixture_program_inside_foreign_table():
    """«Бета» начинается внутри таблицы с текстом «Альфы» — программа по названию из п. 2.3, а не по таблице."""
    _, _, pc = _fixture_parsed()
    beta = [r for r in pc.clinics if r.program == "Бета"]
    assert [r.source_ref for r in beta] == ["c99.program#t1r3", "c99.program#t1r4"] and beta[0].by_referral and not beta[1].by_referral
    assert beta[1].city is None
    alpha_text = [c for c in pc.clauses if c.program == "Альфа" and c.doc_id == "c99.program"]
    beta_text = [c for c in pc.clauses if c.program == "Бета" and c.doc_id == "c99.program"]
    assert alpha_text and beta_text
    assert all(c.id.startswith("c99.program#t1r0") for c in alpha_text)
    assert all(c.id.startswith("c99.program#t2r0") for c in beta_text)


def test_fixture_program_paths_and_extensions():
    _, _, pc = _fixture_parsed()
    by_path = {c.path: c for c in pc.clauses if c.number == "1.4" and c.program == "Альфа"}
    biz = "Программа ДМС › Альфа › Программа стоматологического обслуживания «Бизнес» › Программа включает › 1.4"
    assert "ТЗО" in by_path[biz].text
    tick = ("Программа ДМС › Альфа › Программа добровольного медицинского страхования «Антиклещ» › "
            "По настоящей Программе страховыми случаями являются › 1.4")
    assert "клеща" in by_path[tick].text
    excl = [c for c in pc.clauses if c.program == "Альфа" and c.number == "1" and "Замена пломб" in c.text]
    assert excl[0].path.endswith("«Бизнес» › Исключения из программы › 1")
    ext = [c for c in pc.clauses if c.path.endswith("Расширения к холдингу")]
    assert ext and all(c.program is None for c in ext)
    # одинаковый текст в разных программах — одинаковый text_hash (дедуп делает store)
    h = {c.program: c.text_hash for c in pc.clauses if c.text == "1.1. Первичный осмотр врача."}
    assert h["Альфа"] == h["Бета"]


def test_fixture_care_matrix_absence():
    _, _, pc = _fixture_parsed()
    m = {(x.program, x.care_type): x for x in pc.care}
    assert m[("Альфа", "Стоматология")].present and m[("Альфа", "Врач на дом")].present
    # нет ни в перечне, ни в заголовках «что входит» (упоминание в «Помощи путешествующим» и в заголовке
    # исключений не считается): present=False, ссылки — перечень программы и её текст условий
    absent = m[("Бета", "Стоматология")]
    assert not absent.present and absent.source_refs == ["c99.program#t1r1", "c99.program#t2r0p0"]
    # нет в перечне, но есть раздел текста «Вызов врача на дом…:» → present=True со ссылкой на этот пункт
    home = m[("Бета", "Врач на дом")]
    clause = {c.id: c for c in pc.clauses}[home.source_refs[0]]
    assert home.present and len(home.source_refs) == 1 and clause.text.startswith("Вызов врача на дом")
    # ссылки на текст условий — живые id пунктов; на перечень — строка таблицы этой программы «<doc_id>#t…r…»
    refs = [r for x in pc.care for r in x.source_refs]
    assert {r for r in refs if "p" in r.rsplit("#", 1)[1]} <= {c.id for c in pc.clauses}
    assert all(r.startswith("c99.program#t") for r in refs)


# ------------------------------------------------------------------ комплект организаторов

def _gk2():
    from dms_core.kit import scan

    root = kit_or_skip()
    groups = {g.id: g for g in scan(root)}
    if "gk2" not in groups:
        skip("в комплекте нет ГК 2")
    return root, groups["gk2"].contracts


@cache
def _parsed(cid: str):
    root, contracts = _gk2()
    kc = next(c for c in contracts if c.id == cid)
    return ren.parse(kc, root)


EXPECTED = {   # gk2.md §1: численность п. 1.1, премия п. 2.4, программ в п. 2.3
    "c05": (67, 2_369_265.0, 34), "c06": (47, 1_735_771.0, 27), "c07": (23, 793_764.0, 25),
    "c08": (90, 3_366_809.0, 37),
}


def test_kit_detect_only_gk2():
    from dms_core.kit import scan

    root = kit_or_skip()
    for g in scan(root):
        for kc in g.contracts:
            assert ren.matches(kc, root) == (g.id == "gk2"), kc.id


def test_kit_premiums_and_card():
    _gk2()
    for cid, (persons, total, n_prog) in EXPECTED.items():
        pc = _parsed(cid)
        c = pc.contract
        assert len(c.programs) == n_prog and c.headcount == persons, cid
        assert sum(p.persons for p in c.programs) == persons, cid
        assert round(sum(p.premium_per_person * p.persons for p in c.programs), 2) == total == ren.premium_total(pc)
        assert round(sum(x.amount for x in c.payments), 2) == total and len(c.payments) == 4, cid
        assert str(c.period_from) == "2026-04-25" and str(c.period_to) == "2027-04-24", cid
        assert pc.warnings == [], (cid, pc.warnings[:3])


def test_kit_clinics_match_reference_rows():
    _gk2()
    if not REF.is_dir():
        skip(f"нет эталона {REF}")
    for cid in EXPECTED:
        pc = _parsed(cid)
        ref = json.loads((REF / f"rows{cid[-1]}.json").read_text(encoding="utf-8"))
        want = {f"{cid}.program#t{r['t']}r{r['r']}": (r["prog"], r["sec"], r["org"], r["addr"], r["code"]) for r in ref}
        got = {r.source_ref: (r.program, r.care_type, r.clinic_name, r.address or "", r.code or "")
               for r in pc.clinics}
        assert got == want, cid
        notes = {f"{cid}.program#t{r['t']}r{r['r']}" for r in ref if r.get("note")}
        assert {r.source_ref for r in pc.clinics if r.access_note and "Внимание" in r.access_note} == notes, cid
        assert {r.program for r in pc.clinics} == {p.name for p in pc.contract.programs}, cid


def test_kit_contract_ids_and_unique_ids():
    _gk2()
    for cid in EXPECTED:
        pc = _parsed(cid)
        assert all(c.contract_id == cid and c.doc_id.startswith(cid + ".") for c in pc.clauses), cid
        assert all(r.contract_id == cid and r.doc_id == f"{cid}.program" for r in pc.clinics), cid
        assert all(m.contract_id == cid for m in pc.care), cid
        ids = [c.id for c in pc.clauses] + [r.id for r in pc.clinics]
        assert len(ids) == len(set(ids)), cid


def test_kit_icons_c05():
    """gk2.md §2: в дог. 5 иконка «по записи» у 1 586 строк, включая все строки «Госпитализации»."""
    _gk2()
    pc = _parsed("c05")
    assert sum(r.by_referral for r in pc.clinics) == 1586
    assert all(r.by_referral for r in pc.clinics if r.care_type == "Госпитализация")


def test_kit_care_matrix_barnaul_and_vip():
    """К12: в «Барнауле» нет стоматологии — present=False со ссылкой; К13: ММЦ МЕДСИ на Красной Пресне — только ВИП."""
    _gk2()
    pc = _parsed("c05")
    m = {(x.program, x.care_type): x for x in pc.care}
    assert not m[("Барнаул", "Стоматология")].present and m[("Барнаул", "Стоматология")].source_refs
    assert m[("Барнаул", "Поликлиника")].present and m[("Барнаул", "Госпитализация")].present
    assert m[("Москва", "Стоматология")].present
    assert not m[("Барнаул", "Скорая помощь")].present            # нет ни в перечне, ни в разделах текста
    assert "ВИП" not in {p.name for p in pc.contract.programs}
    c06 = _parsed("c06")
    pres = {r.program for r in c06.clinics if "Красной Пресне" in r.clinic_name}
    assert pres == {"ВИП"}


def test_kit_care_matrix_text_overrides_absence():
    """Ревизия волны 1: «нет в документах» только если вида помощи нет ни в перечне клиник, ни в разделах текста
    условий. «Саров» (c08): клиник «Госпитализации» нет, но в тексте есть раздел «Стационарное лечение
    предусматривает:» (п. 1.7 «Антиклещ») → present=True со ссылкой на текст; стоматологии, врача на дом и скорой
    нет ни там, ни там → False. Москва (c05): клиник скорой нет, раздел «Круглосуточная скорая…» есть → True."""
    _gk2()
    pc = _parsed("c08")
    m = {(x.program, x.care_type): x for x in pc.care}
    clause = {c.id: c for c in pc.clauses}
    hosp = m[("Саров", "Госпитализация")]
    assert hosp.present and "c08.program#t51r0p19" in hosp.source_refs
    assert clause["c08.program#t51r0p19"].text.startswith("Стационарное лечение предусматривает")
    assert not any(r.program == "Саров" and r.care_type == "Госпитализация" for r in pc.clinics)
    for ct in ("Стоматология", "Врач на дом", "Скорая помощь"):
        assert not m[("Саров", ct)].present, ct
    assert m[("Саров", "Поликлиника")].present
    c05 = {(x.program, x.care_type): x for x in _parsed("c05").care}
    assert c05[("Москва", "Скорая помощь")].present
    for cid in EXPECTED:
        p = _parsed(cid)
        ids = {c.id for c in p.clauses}
        for x in p.care:
            text_refs = [r for r in x.source_refs if "p" in r.rsplit("#", 1)[1]]
            assert set(text_refs) <= ids, (cid, x)
            assert all(r.startswith(f"{cid}.program#") for r in x.source_refs), (cid, x)
            if not x.present:   # у отсутствия две ссылки: перечень программы и её текст условий
                assert len(x.source_refs) == 2 and len(text_refs) == 1, (cid, x)


def test_kit_program_text_dedup_15_variants():
    """gk2.md §3: тексты условий 123 программ ГК сводятся к 15 вариантам (после нормализации номеров сносок)."""
    import hashlib
    import re

    _gk2()
    variants = set()
    n_blocks = 0
    for cid in EXPECTED:
        by: dict[str, list[str]] = {}
        for c in _parsed(cid).clauses:
            if c.program and c.doc_id.endswith(".program") and "#t" in c.id:
                by.setdefault(c.program, []).append(re.sub(r"\s+", " ", c.text))
        n_blocks += len(by)
        for lines in by.values():
            s = "\n".join(lines)
            s = re.sub(r"(\.)\d(\s|$)", r"\1\2", s)            # номер сноски после точки
            s = re.sub(r"(?m)^\d(?=[А-ЯA-Z])", "", s)          # номер сноски в начале строки сноски
            variants.add(hashlib.md5(s.encode()).hexdigest())
    assert n_blocks == 123 and len(variants) == 15
