"""Поиск по перечню программы (tools/clinics.search_clinics, этап w7-fix): служебные слова запроса отбрасываются,
порядок — по числу найденных слов, числа обязательны, «3-й» — число, опечатки, латиница, одна строка на клинику по
одному адресу. Синтетика (перечень tests/guided/test_w7_fix.py), без сети."""
from __future__ import annotations

from dms_core.tools.clinics import search_clinics
from guided.test_w7_fix import DENT, POLY, ST

ROWS = ST.clinics("c01", program="Стандарт")


def _names(q: str, **kw) -> list[str]:
    return [r.clinic_name for r in search_clinics(ROWS, q, **kw)]


def test_service_words_dropped_and_ranked_by_found_words():
    # «клиника» — не слово запроса; равное число найденных слов — порядок перечня
    assert _names("клиника медси") == ["КДЦ МЕДСИ на Белорусской", "МЕДСИ"]
    assert _names("медси на 1905 года") == ["МЕДСИ"]
    assert _names("улица 1905 года") == ["МЕДСИ"]
    got = search_clinics(ROWS, "доктор рядом на кулакова")
    assert "Кулакова" in (got[0].address or "") and got[0].clinic_name == '"Доктор рядом" ООО'
    assert len([r for r in got if r.clinic_name == '"Доктор рядом" ООО']) == 2      # второй филиал — ниже
    assert _names("проспект мира") == ["СМ-Клиника"]
    assert _names("3-й мытищинской") == ["Клиника на Мытищинской"]


def test_typos_latin_and_numbers_required():
    assert _names("медсы") == ["КДЦ МЕДСИ на Белорусской", "МЕДСИ"]
    assert _names("будь здоровь")[0] == '"Клиника Будь Здоров" ООО (Будь здоров)'   # оба слова — первой
    assert _names("zdorovie")[0].startswith('"ЗДОРОВЬЕ"')
    assert _names("medikus") == ['Medikus (ООО "МЕДАВТО")']
    assert _names("медси 1906") == []                   # число запроса обязательно
    assert _names("несуществующая") == []


def test_same_clinic_same_address_one_row_with_question_care():
    medsi = [r for r in ROWS if r.clinic_name == "МЕДСИ"]
    assert len(medsi) == 2 and {r.care_type for r in medsi} == {POLY, DENT}
    got = search_clinics(ROWS, "медси 1905")
    assert len(got) == 1 and got[0].care_type == POLY            # без вида помощи — первая по перечню
    got = search_clinics(ROWS, "медси 1905", care=DENT)
    assert len(got) == 1 and got[0].care_type == DENT            # строка с видом помощи вопроса
    assert len(search_clinics(ROWS, "")) == len(ROWS) - 1        # пустой запрос — весь перечень без повторов
