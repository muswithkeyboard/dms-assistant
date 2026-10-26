"""API разметки таблиц после ревизии (этап w9-fix, CONTRACTS §10): подозрительная разметка (таблица премий) —
предупреждение в пробном разборе и 422 mapping_suspicious без confirm_suspicious; разметка без источника программы —
422 mapping_no_program; у распознанной СК — 409 mapping_not_generic; отрицательная колонка — понятное сообщение;
в списке таблиц — applied_profile, suspicious, similar_unmapped. Свой маленький договор «Ренессанса»."""
from __future__ import annotations

from test_api_mapping import _wait

from api.api_kit import FakeEngine, make_client
from dms_core.store import KitStore
from fixtures import client_kit as ck


def _ren_client(c, tmp, *, force_generic=True):
    r = c.post("/api/clients", json={"name": "Клиент Р", "contracts": [
        {"legal_entity": "ООО Р", "force_generic": force_generic, "insurer_name": "Ренессанс"}]})
    assert r.status_code == 201, r.text
    cid = r.json()["contracts"][0]["id"]
    for f in ck.ren_files(tmp / cid):
        doc_type = "program" if "Программа" in f.name else "contract"
        r = c.post(f"/api/clients/contracts/{cid}/files", files={"file": (f.name, f.read_bytes())},
                   data={"doc_type": doc_type})
        assert r.status_code == 201, r.text
    job = _wait(c, c.post(f"/api/clients/contracts/{cid}/process").json()["id"])
    assert job["state"] == "done", job
    return cid, job


def test_premium_table_is_suspicious_and_needs_confirm_and_program():
    tmp = ck.workdir()
    c, _svc, _ = make_client(tmp, engine=FakeEngine(), kit=KitStore(tmp / "derived" / "kit.sqlite"))
    cid, job = _ren_client(c, tmp)
    assert job["report"]["adapter"] == "generic"
    tables = c.get(f"/api/clients/contracts/{cid}/tables").json()
    assert all({"applied_profile", "suspicious", "similar_unmapped"} <= set(t) for t in tables)
    premium = next(t for t in tables if t["ref"]["doc_id"].endswith(".contract"))
    url = f"/api/clients/contracts/{cid}/tables"
    mapping = {"header_row": 0, "columns": {"0": "clinic_name"}, "program": "Альфа"}
    r = c.post(url + "/try", json={"ref": premium["ref"], "mapping": mapping})
    assert r.status_code == 200 and r.json()["suspicious"], r.text
    assert "похожа на ошибочную" in r.json()["warnings"][0]
    r = c.put(url + "/mapping", json={"ref": premium["ref"], "mapping": mapping})
    assert r.status_code == 422 and r.json()["error"]["code"] == "mapping_suspicious", r.text
    assert "сумм" in r.json()["error"]["message"]
    # без источника программы — 422 mapping_no_program (раньше — «программа договора» «Перечень клиник договора»)
    r = c.put(url + "/mapping", json={"ref": premium["ref"], "mapping": {"header_row": 0, "columns": {"0": "clinic_name"}},
                                      "confirm_suspicious": True})
    assert r.status_code == 422 and r.json()["error"]["code"] == "mapping_no_program", r.text
    r = c.put(url + "/mapping", json={"ref": premium["ref"], "mapping": mapping, "confirm_suspicious": "да"})
    assert r.status_code == 422 and r.json()["error"]["code"] == "invalid"
    r = c.put(url + "/mapping", json={"ref": premium["ref"], "mapping": mapping, "confirm_suspicious": True})
    assert r.status_code == 200, r.text
    assert r.json()["preview"]["suspicious"]                    # в списке таблиц — пометка «не похоже на перечень»
    # отрицательный номер колонки — сообщение с этим номером, а не «Колонка 0»
    r = c.post(url + "/try", json={"ref": premium["ref"], "mapping": {"columns": {"-1": "address", "0": "clinic_name"}}})
    assert r.status_code == 422 and r.json()["error"]["code"] == "mapping_bad_column"
    assert "-1" in r.json()["error"]["message"]


def test_mapping_rejected_for_recognized_insurer():
    tmp = ck.workdir()
    c, _svc, _ = make_client(tmp, engine=FakeEngine(), kit=KitStore(tmp / "derived" / "kit.sqlite"))
    cid, job = _ren_client(c, tmp, force_generic=False)
    assert job["report"]["adapter"] == "renaissance"
    tables = c.get(f"/api/clients/contracts/{cid}/tables").json()
    t = tables[0]
    r = c.put(f"/api/clients/contracts/{cid}/tables/mapping",
              json={"ref": t["ref"], "mapping": {"header_row": 0, "columns": {"0": "clinic_name"}, "program": "Альфа"}})
    assert r.status_code == 409 and r.json()["error"]["code"] == "mapping_not_generic", r.text
