"""GET /api/requests/{id}/clinic-candidates (этап w7-fix): одна клиника по одному адресу — одна строка выдачи,
total — по уникальным, из строк разных видов помощи — вида помощи лучшего кандидата ответа. Синтетика, без сети."""
from __future__ import annotations

from api.api_kit import make_client, new_request
from guided.test_w7_fix import DENT, POLY, _kit


def _ask(c, rid: str, text: str) -> dict:
    r = c.post(f"/api/requests/{rid}/messages", json={"text": text})
    assert r.status_code == 200, r.text
    return r.json()["answer"]


def test_search_collapses_same_clinic_same_address_with_question_care():
    c, svc, _tmp = make_client(kit=_kit())
    _chat, rid = new_request(c, {"contract_id": "c01", "program": "Стандарт"})
    url = f"/api/requests/{rid}/clinic-candidates"
    ans = _ask(c, rid, "Можно ли к неврологу в МЕДСИ на 1905 года?")
    assert ans["clinic_choices"] and ans["clinic_choices"][0]["candidates"][0]["care_type"] == POLY
    out = c.get(url, params={"contract_id": "c01", "q": "медси 1905"}).json()
    assert out["total"] == 1 and out["rows"][0]["care_type"] == POLY
    every = svc.kit.clinics("c01", program="Стандарт")
    assert c.get(url, params={"contract_id": "c01"}).json()["total"] == len(every) - 1
    ans = _ask(c, rid, "Можно ли вылечить кариес у стоматолога в МЕДСИ на 1905 года?")
    assert ans["clinic_choices"][0]["candidates"][0]["care_type"] == DENT
    out = c.get(url, params={"contract_id": "c01", "q": "медси 1905"}).json()
    assert out["total"] == 1 and out["rows"][0]["care_type"] == DENT
