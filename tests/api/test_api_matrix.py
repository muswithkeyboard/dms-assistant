"""API разметки матрицы «программа × вид помощи» (CONTRACTS §11, ADR 0013): предложение матрицы после разметки
перечня, пробный разбор (пары, пропущенные ячейки), ошибки mapping_no_care / mapping_no_program / mapping_suspicious,
сохранение с обработкой → ready, профиль СК хранит разметку матрицы, derive_care в пробном разборе перечня."""
from __future__ import annotations

import time

from api.api_kit import FakeEngine, make_client
from dms_core.store import KitStore
from fixtures import client_kit as ck
from fixtures import mapping_fixture as mf

LIST = {"header_row": 0, "columns": {"0": "program", "1": "clinic_name", "2": "address", "3": "care_type"}}
WIDE = {"role": "care_matrix", "matrix_layout": "wide", "header_row": 0, "columns": {"0": "care_type"},
        "program_cols": {"1": "Стандарт", "2": "Премиум"}}


def _wait(c, job_id):
    for _ in range(400):
        job = c.get(f"/api/clients/jobs/{job_id}").json()
        if job["state"] in ("done", "error"):
            return job
        time.sleep(0.05)
    raise AssertionError("задание не закончилось")


def _client(c, tmp):
    r = c.post("/api/clients", json={"name": "Клиент Матрица", "contracts": [
        {"legal_entity": "ООО Матрица", "force_generic": True, "insurer_name": "СК Матрица"}]})
    assert r.status_code == 201, r.text
    cid = r.json()["contracts"][0]["id"]
    src = mf.matrix_docx(tmp / "src" / cid / "Договор.docx")
    r = c.post(f"/api/clients/contracts/{cid}/files", files={"file": ("Договор.docx", src.read_bytes())},
               data={"doc_type": "contract"})
    assert r.status_code == 201, r.text
    assert _wait(c, c.post(f"/api/clients/contracts/{cid}/process").json()["id"])["state"] == "done"
    return cid


def test_matrix_try_put_errors_process_and_profile():
    tmp = ck.workdir()
    c, _svc, _ = make_client(tmp, engine=FakeEngine(), kit=KitStore(tmp / "derived" / "kit.sqlite"))
    cid = _client(c, tmp)
    lst, wide = c.get(f"/api/clients/contracts/{cid}/tables").json()
    # перечень с флажком «виды помощи → матрица»: пробный разбор показывает и пары матрицы
    r = c.post(f"/api/clients/contracts/{cid}/tables/try",
               json={"ref": lst["ref"], "mapping": {**LIST, "derive_care": True}})
    assert r.status_code == 200 and r.json()["total"] == 4 and r.json()["matrix_total"] == 4, r.text
    r = c.put(f"/api/clients/contracts/{cid}/tables/mapping", json={"ref": lst["ref"], "mapping": LIST,
                                                                    "process": True})
    assert r.status_code == 202, r.text
    job = _wait(c, r.json()["job"]["id"])
    assert job["report"]["readiness"] == "limited" and job["report"]["counts"]["mapped_matrices"] == 0

    # программы договора известны — экран предлагает широкую матрицу
    wide = c.get(f"/api/clients/contracts/{cid}/tables").json()[1]
    assert wide["suggested"]["role"] == "care_matrix" and wide["suggested"]["matrix_layout"] == "wide"
    assert wide["suggested"]["program_cols"] == {"1": "Стандарт", "2": "Премиум"}

    r = c.post(f"/api/clients/contracts/{cid}/tables/try", json={"ref": wide["ref"], "mapping": WIDE})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["total"] == 7 and not body["suspicious"]
    assert {"program": "Стандарт", "care_type": "Стоматологическая помощь", "present": False,
            "source_ref": f"{cid}.contract#t1r2c1"} in body["matrix"]
    assert body["skipped"] == [{"cell": "строка 5, колонка 2", "text": "до 100 000 руб. в год"}]
    assert body["skipped_total"] == 1 and any("не «да» / «нет»" in w for w in body["warnings"])

    for mapping, code in (({**WIDE, "columns": {}}, "mapping_no_care"),
                          ({**WIDE, "program_cols": {}}, "mapping_no_program"),
                          ({**WIDE, "program_cols": {"9": "X"}}, "mapping_bad_column")):
        r = c.put(f"/api/clients/contracts/{cid}/tables/mapping", json={"ref": wide["ref"], "mapping": mapping})
        assert r.status_code == 422 and r.json()["error"]["code"] == code, r.text
    # перечень, размеченный как матрица, — подозрительно: сохранить можно только с подтверждением
    bad = {"role": "care_matrix", "matrix_layout": "wide", "header_row": 0, "columns": {"1": "care_type"},
           "program_cols": {"2": "Стандарт", "3": "Премиум"}}
    r = c.post(f"/api/clients/contracts/{cid}/tables/try", json={"ref": lst["ref"], "mapping": bad})
    assert r.status_code == 200 and r.json()["suspicious"], r.text
    r = c.put(f"/api/clients/contracts/{cid}/tables/mapping", json={"ref": lst["ref"], "mapping": bad})
    assert r.status_code == 422 and r.json()["error"]["code"] == "mapping_suspicious"
    assert "матрица видов помощи" in r.json()["error"]["message"]

    r = c.put(f"/api/clients/contracts/{cid}/tables/mapping",
              json={"ref": wide["ref"], "mapping": WIDE, "save_profile": True, "process": True})
    assert r.status_code == 202, r.text
    prof = r.json()["profile"]
    # профиль: программы, равные тексту заголовка, — «как в заголовке» (в следующем договоре — свои)
    assert prof["mapping"]["role"] == "care_matrix" and prof["mapping"]["program_cols"] == {"1": "", "2": ""}
    job = _wait(c, r.json()["job"]["id"])
    rep = job["report"]
    assert rep["readiness"] == "ready" and rep["counts"]["care_rows"] == 7 and rep["counts"]["mapped_matrices"] == 1
    t = c.get(f"/api/clients/contracts/{cid}/tables").json()[1]
    assert t["mapping"]["role"] == "care_matrix" and t["suspicious"] == []


def test_matrix_swapped_columns_and_list_conflict_api():
    """Этап w10-fix2: при первой обработке экран предлагает широкую матрицу (программы — заголовки); колонки программ
    перепутаны — try предупреждает, PUT без confirm_suspicious — 422; «нет» матрицы при клиниках этого вида в
    перечне — предупреждение пробного разбора."""
    tmp = ck.workdir()
    c, _svc, _ = make_client(tmp, engine=FakeEngine(), kit=KitStore(tmp / "derived" / "kit.sqlite"))
    r = c.post("/api/clients", json={"name": "Клиент Атака", "contracts": [
        {"legal_entity": "ООО А", "force_generic": True, "insurer_name": "СК Атака"}]})
    cid = r.json()["contracts"][0]["id"]
    src = mf.attack_docx(tmp / "src" / cid / "Договор.docx")
    c.post(f"/api/clients/contracts/{cid}/files", files={"file": ("Договор.docx", src.read_bytes())},
           data={"doc_type": "contract"})
    assert _wait(c, c.post(f"/api/clients/contracts/{cid}/process").json()["id"])["state"] == "done"
    lst, wide, _serv = c.get(f"/api/clients/contracts/{cid}/tables").json()
    assert wide["suggested"]["role"] == "care_matrix" and wide["suggested"]["program_cols"] == {"1": "", "2": ""}
    r = c.put(f"/api/clients/contracts/{cid}/tables/mapping", json={"ref": lst["ref"], "mapping": lst["suggested"],
                                                                    "process": True})
    assert r.status_code == 202 and _wait(c, r.json()["job"]["id"])["state"] == "done"
    swap = {**wide["suggested"], "program_cols": {"1": "Барнаул", "2": "Москва"}}
    r = c.post(f"/api/clients/contracts/{cid}/tables/try", json={"ref": wide["ref"], "mapping": swap})
    assert r.status_code == 200 and any("перепутаны" in x for x in r.json()["suspicious"]), r.text
    r = c.put(f"/api/clients/contracts/{cid}/tables/mapping", json={"ref": wide["ref"], "mapping": swap})
    assert r.status_code == 422 and r.json()["error"]["code"] == "mapping_suspicious"
    r = c.post(f"/api/clients/contracts/{cid}/tables/try", json={"ref": wide["ref"], "mapping": wide["suggested"]})
    body = r.json()
    assert not body["suspicious"] and any("матрица говорит «нет», а в перечне есть клиники" in w
                                          for w in body["warnings"]), r.text
    r = c.put(f"/api/clients/contracts/{cid}/tables/mapping", json={"ref": wide["ref"], "mapping": swap,
                                                                    "confirm_suspicious": True})
    assert r.status_code == 200
