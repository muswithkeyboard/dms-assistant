"""clinics_pick: та же программа + вид помощи; ранжирование A6 — без франшизы → рядом → та же сеть; до k штук."""
from __future__ import annotations

from dms_core.scenarios.clinics_pick import pick
from dms_core.schemas import ClinicRow
from dms_core.store import KitStore
from fixtures.synth_kit import make_parsed


def _row(i: int, name: str, *, program="Стандарт", care="Стоматологическая помощь", franchise=None, city="Москва",
         legal=None, address=None, referral=False, note=None) -> ClinicRow:
    return ClinicRow(id=f"c01.clinics#x{i}", contract_id="c01", doc_id="c01.clinics", program=program, care_type=care,
                     clinic_name=name, legal_name=legal, address=address or f"г. {city}, ул. {i}", city=city,
                     franchise_pct=franchise, by_referral=referral, access_note=note, source_ref=f"x:r{i}")


def _store() -> KitStore:
    p = make_parsed("c01")       # там «Улыбка» с франшизой 10 % по всем видам помощи
    p.clinics += [
        _row(1, "Дент Плюс", franchise=20.0, city="Химки"),
        _row(2, "Зуб Мастер", franchise="—", city="Химки", legal="ООО «Сеть Зубов»"),
        _row(3, "Белый Зуб", franchise="—", city="Москва", legal="ООО «Сеть Зубов»"),
        _row(4, "Дентал Центр", franchise="—", city="Москва", referral=True, note="только взрослые"),
        _row(5, "Чужая программа", program="Премиум", franchise="—"),
        _row(6, "Терапия", care="Амбулаторно-поликлиническая помощь", franchise="—"),
        _row(7, "Дент Плюс", franchise=20.0, city="Химки", address="г. Химки, ул. 1"),   # дубль строки 1
        _row(8, "Неизвестно", city="Москва"),
    ]
    st = KitStore(":memory:")
    st.save(p)
    st.save(make_parsed("c02"))
    return st


def test_pick_filters_program_and_care_and_ranks():
    st = _store()
    alts = pick("c01", "Стандарт", "dental", near="Москва", store=st, exclude="Улыбка", network="Сеть Зубов", k=10)
    names = [a.clinic.clinic_name for a in alts]
    assert "Чужая программа" not in names and "Терапия" not in names and "Улыбка" not in names
    # без франшизы: Москва раньше Химок; внутри Москвы — та же сеть раньше
    assert names[:3] == ["Белый Зуб", "Дентал Центр", "Зуб Мастер"]
    assert names[3] == "Неизвестно"              # размер франшизы не указан — после «без франшизы»
    assert names[-1] == "Дент Плюс" and names.count("Дент Плюс") == 1   # дубль строки убран
    assert all(a.clinic.contract_id == "c01" for a in alts)


def test_reason_has_conditions_and_source():
    st = _store()
    alts = pick("c01", "Стандарт", "стоматология", store=st)
    by = {a.clinic.clinic_name: a.reason for a in alts}
    assert "по направлению" in by["Дентал Центр"] and "только взрослые" in by["Дентал Центр"]
    assert "франшиза не применяется" in by["Белый Зуб"] and "источник: c01.clinics, строка x:r3" in by["Белый Зуб"]
    assert "франшиза 10 %" in by["Клиника «Улыбка»"]
    assert len(alts) == 5                          # k по умолчанию


def test_pick_needs_program_and_care():
    st = _store()
    assert pick("c01", "", "dental", store=st) == [] and pick("c01", "Стандарт", "", store=st) == []


def test_pick_never_returns_other_contract():
    st = _store()
    alts = pick("c02", "Стандарт", "dental", store=st, k=10)
    assert alts and all(a.clinic.contract_id == "c02" for a in alts)
