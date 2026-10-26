"""API разметки таблиц (CONTRACTS §10, ADR 0012): таблицы договора, пробный разбор, сохранение разметки и профиля,
обработка, удаление, профили СК, ошибки по-русски, журнал без имён файлов и текстов."""
from __future__ import annotations

import logging
import time

from api.api_kit import FakeEngine, make_client
from dms_core.store import KitStore
from fixtures import client_kit as ck
from fixtures import mapping_fixture as mf

MAP = {"header_row": 0, "columns": {"1": "clinic_name", "2": "address", "3": "care_type"}, "program": "Стандарт"}


def _wait(c, job_id):
    for _ in range(400):
        job = c.get(f"/api/clients/jobs/{job_id}").json()
        if job["state"] in ("done", "error"):
            return job
        time.sleep(0.05)
    raise AssertionError("задание не закончилось")


def _processed_client(c, tmp, *, insurer_name=None, legal="ООО Ромашка", gid=None):
    if gid is None:
        body = {"name": "Клиент М", "contracts": [{"legal_entity": legal, "force_generic": True,
                                                   **({"insurer_name": insurer_name} if insurer_name else {})}]}
        r = c.post("/api/clients", json=body)
        assert r.status_code == 201, r.text
        gid, cid = r.json()["id"], r.json()["contracts"][0]["id"]
        assert r.json()["contracts"][0]["force_generic"] is True
    else:
        r = c.post(f"/api/clients/{gid}/contracts", json={"legal_entity": legal, "force_generic": True,
                                                          **({"insurer_name": insurer_name} if insurer_name else {})})
        assert r.status_code == 201, r.text
        cid = r.json()["id"]
    src = mf.contract_docx(tmp / "src" / cid / "Договор.docx")
    r = c.post(f"/api/clients/contracts/{cid}/files", files={"file": ("Договор.docx", src.read_bytes())},
               data={"doc_type": "contract"})
    assert r.status_code == 201, r.text
    job = _wait(c, c.post(f"/api/clients/contracts/{cid}/process").json()["id"])
    assert job["state"] == "done", job
    return gid, cid, job


def test_tables_try_put_process_profile_delete():
    tmp = ck.workdir()
    c, _svc, _ = make_client(tmp, engine=FakeEngine(), kit=KitStore(tmp / "derived" / "kit.sqlite"))
    gid, cid, job = _processed_client(c, tmp, insurer_name="СК Тест")
    assert job["report"]["counts"]["mapped_tables"] == 0

    tables = c.get(f"/api/clients/contracts/{cid}/tables").json()
    assert len(tables) == 1
    t = tables[0]
    assert t["ref"] == {"doc_id": f"{cid}.contract", "kind": "docx_table", "index": 1, "sheet": None}
    assert t["n_rows"] == 9 and t["n_cols"] == 6 and t["mapping"] is None and t["profile_id"] is None
    assert t["suggested"]["columns"]["1"] == "clinic_name" and t["signature"].startswith("наименование лпу|")

    r = c.post(f"/api/clients/contracts/{cid}/tables/try", json={"ref": t["ref"], "mapping": MAP})
    assert r.status_code == 200, r.text
    assert r.json()["total"] == 6 and r.json()["rows"][0]["clinic_name"] == "Клиника Ромашка"
    assert c.get(f"/api/clients/contracts/{cid}/tables").json()[0]["mapping"] is None    # пробный — не сохраняет

    # ошибки по контракту
    for mapping, code in (({"columns": {"2": "address"}}, "mapping_no_name"),
                          ({"columns": {"1": "clinic_name", "9": "address"}}, "mapping_bad_column"),
                          ({"columns": {"1": "кот"}}, "invalid")):
        r = c.post(f"/api/clients/contracts/{cid}/tables/try", json={"ref": t["ref"], "mapping": mapping})
        assert r.status_code == 422 and r.json()["error"]["code"] == code, r.text
    r = c.post(f"/api/clients/contracts/{cid}/tables/try",
               json={"ref": {**t["ref"], "index": 7}, "mapping": MAP})
    assert r.status_code == 422 and r.json()["error"]["code"] == "table_not_found"

    r = c.put(f"/api/clients/contracts/{cid}/tables/mapping", json={"ref": t["ref"], "mapping": MAP,
                                                                     "save_profile": True})
    assert r.status_code == 200, r.text
    prof = r.json()["profile"]
    assert prof["id"] == "ip1" and prof["insurer_name"] == "СК Тест" and r.json()["preview"]["mapping"]["program"]
    assert [p["id"] for p in c.get("/api/insurer-profiles").json()] == ["ip1"]

    r = c.put(f"/api/clients/contracts/{cid}/tables/mapping", json={"ref": t["ref"], "mapping": MAP,
                                                                     "process": True})
    assert r.status_code == 202, r.text
    assert set(r.json()) == {"preview", "profile", "job", "applied_tables"} and r.json()["applied_tables"] == 1
    rep = _wait(c, r.json()["job"]["id"])["report"]
    assert rep["counts"]["mapped_tables"] == 1 and rep["counts"]["clinics"] == 6 and rep["readiness"] == "limited"

    # договор без СК в карточке: профиль только предлагается (w9-fix п. 2а), applied_profile нет
    _g, cid0, job0 = _processed_client(c, tmp, legal="ООО Без СК", gid=gid)
    assert job0["report"]["counts"]["mapped_tables"] == 0
    t0 = c.get(f"/api/clients/contracts/{cid0}/tables").json()[0]
    assert t0["profile_id"] == "ip1" and t0["applied_profile"] is None and t0["suggested"]
    # второй договор той же СК: профиль по подписи применяется при обработке сам («размечено профилем»)
    _g, cid2, job2 = _processed_client(c, tmp, legal="ООО Лютик", gid=gid, insurer_name="СК «Тест»")
    assert job2["report"]["counts"]["mapped_tables"] == 1
    t2 = c.get(f"/api/clients/contracts/{cid2}/tables").json()[0]
    assert t2["profile_id"] == "ip1" and t2["applied_profile"] == "ip1" and t2["suspicious"] == []
    assert c.get("/api/insurer-profiles").json()[0]["used"] == 1

    ref = t["ref"]
    r = c.delete(f"/api/clients/contracts/{cid}/tables/mapping?doc_id={ref['doc_id']}&kind=docx_table&index=1")
    assert r.status_code == 204
    assert c.get(f"/api/clients/contracts/{cid}/tables").json()[0]["mapping"] is None
    assert c.delete("/api/insurer-profiles/ip1").status_code == 204
    r = c.delete("/api/insurer-profiles/ip1")
    assert r.status_code == 404 and r.json()["error"]["message"]


def test_save_profile_needs_insurer_name_and_unprocessed_has_no_tables():
    tmp = ck.workdir()
    c, _svc, _ = make_client(tmp, engine=FakeEngine(), kit=KitStore(tmp / "derived" / "kit.sqlite"))
    r = c.post("/api/clients", json={"name": "К", "contracts": [{"legal_entity": "ООО"}]})
    cid = r.json()["contracts"][0]["id"]
    assert r.json()["contracts"][0]["force_generic"] is False
    assert c.get(f"/api/clients/contracts/{cid}/tables").json() == []
    r = c.post("/api/clients", json={"name": "К", "contracts": [{"legal_entity": "ООО", "force_generic": "да"}]})
    assert r.status_code == 422 and r.json()["error"]["field"] == "contracts.0.force_generic"
    _gid, cid, _job = _processed_client(c, tmp)
    ref = c.get(f"/api/clients/contracts/{cid}/tables").json()[0]["ref"]
    r = c.put(f"/api/clients/contracts/{cid}/tables/mapping", json={"ref": ref, "mapping": MAP,
                                                                     "save_profile": True})
    assert r.status_code == 422 and r.json()["error"]["field"] == "insurer_name"
    r = c.put(f"/api/clients/contracts/{cid}/tables/mapping", json={"ref": ref, "mapping": MAP,
                                                                     "save_profile": True, "insurer_name": "СК Новая"})
    assert r.status_code == 200 and r.json()["profile"]["insurer_name"] == "СК Новая"
    assert c.get("/api/clients").json()[1]["contracts"][0]["insurer_name"] == "СК Новая"


def test_mapping_logs_without_file_names_and_texts():
    tmp = ck.workdir()
    c, _svc, _ = make_client(tmp, engine=FakeEngine(), kit=KitStore(tmp / "derived" / "kit.sqlite"))

    records: list[str] = []

    class H(logging.Handler):
        def emit(self, record):
            records.append(record.getMessage())

    h = H()
    logging.getLogger("dms").addHandler(h)
    try:
        _gid, cid, _job = _processed_client(c, tmp)
        ref = c.get(f"/api/clients/contracts/{cid}/tables").json()[0]["ref"]
        c.post(f"/api/clients/contracts/{cid}/tables/try", json={"ref": ref, "mapping": MAP})
        c.put(f"/api/clients/contracts/{cid}/tables/mapping", json={"ref": ref, "mapping": MAP})
    finally:
        logging.getLogger("dms").removeHandler(h)
    text = "\n".join(records)
    assert "Договор.docx" not in text and "Ромашка" not in text and "Лесная" not in text
    assert any("mapping" in x or "tables" in x for x in records)


def _add_program(c, tmp, cid):
    prog = mf.program_tables_docx(tmp / "src" / cid / "Программа.docx")
    r = c.post(f"/api/clients/contracts/{cid}/files", files={"file": ("Программа.docx", prog.read_bytes())},
               data={"doc_type": "program"})
    assert r.status_code == 201, r.text
    job = _wait(c, c.post(f"/api/clients/contracts/{cid}/process").json()["id"])
    assert job["state"] == "done", job
    return job


def test_tables_without_header_apply_same_layout_and_profile_by_layout():
    """Дополнение §10: перечень «по таблице на программу» — предложение «строки заголовка нет» + программа из
    названия, layout / same_layout в списке таблиц, PUT с apply_same_layout размечает все таблицы раскладки."""
    tmp = ck.workdir()
    c, _svc, _ = make_client(tmp, engine=FakeEngine(), kit=KitStore(tmp / "derived" / "kit.sqlite"))
    gid, cid, _job = _processed_client(c, tmp, insurer_name="СК Тест")
    _add_program(c, tmp, cid)

    tables = [t for t in c.get(f"/api/clients/contracts/{cid}/tables").json() if t["ref"]["doc_id"].endswith("program")]
    assert len(tables) == 2 and {t["same_layout"] for t in tables} == {2}
    assert {t["layout"] for t in tables} == {"layout:4:0,2,3"} and tables[0]["signature"] == "layout:4:0,2,3"
    sugg = tables[0]["suggested"]
    assert sugg["header_row"] is None and sugg["program_from_title"] is True
    assert sugg["columns"] == {"0": "clinic_name", "2": "address", "3": "code"}

    r = c.post(f"/api/clients/contracts/{cid}/tables/try", json={"ref": tables[1]["ref"], "mapping": sugg})
    assert r.status_code == 200 and {x["program"] for x in r.json()["rows"]} == {"Тверь"}, r.text
    r = c.put(f"/api/clients/contracts/{cid}/tables/mapping",
              json={"ref": tables[0]["ref"], "mapping": sugg, "apply_same_layout": "да"})
    assert r.status_code == 422
    r = c.put(f"/api/clients/contracts/{cid}/tables/mapping",
              json={"ref": tables[0]["ref"], "mapping": sugg, "apply_same_layout": True, "save_profile": True,
                    "process": True})
    assert r.status_code == 202 and r.json()["applied_tables"] == 2, r.text
    assert r.json()["profile"]["signature"] == "layout:4:0,2,3" and r.json()["preview"]["same_layout"] == 2
    rep = _wait(c, r.json()["job"]["id"])["report"]
    assert rep["counts"]["mapped_tables"] == 2 and rep["counts"]["clinics"] == 6
    assert all(t["mapping"] for t in c.get(f"/api/clients/contracts/{cid}/tables").json()
               if t["ref"]["doc_id"].endswith("program"))

    # следующий договор той же СК: профиль по раскладке размечает обе таблицы программ сам
    _g, cid2, _ = _processed_client(c, tmp, legal="ООО Лютик", gid=gid, insurer_name="СК Тест")
    rep2 = _add_program(c, tmp, cid2)["report"]
    assert rep2["counts"]["mapped_tables"] == 2
