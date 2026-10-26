"""API подключения клиента (CONTRACTS §9, ADR 0011): клиент и договоры, загрузка файлов (сигнатура, очистка имени,
лимиты, замена), PATCH/DELETE, обработка в фоне (одно задание за раз, стадии, отчёт), договор в /api/groups и
чатах, ошибки по-русски, журнал без имён файлов клиента."""
from __future__ import annotations

import logging
import time

from api.api_kit import FakeEngine, make_client
from dms_core import adapters
from dms_core.store import KitStore
from fixtures import client_kit as ck
from helpers import patched


def _client_with_contract(c, insurer=None):
    body = {"name": "Клиент Р", "contracts": [{"legal_entity": "ООО Ромашка", "number": "Р-1"}]}
    if insurer:
        body["contracts"][0]["insurer"] = insurer
    r = c.post("/api/clients", json=body)
    assert r.status_code == 201, r.text
    g = r.json()
    return g["id"], g["contracts"][0]["id"]


def _upload(c, cid, path, name=None, **data):
    return c.post(f"/api/clients/contracts/{cid}/files", files={"file": (name or path.name, path.read_bytes())},
                  data=data)


def _wait(c, job_id):
    for _ in range(400):
        job = c.get(f"/api/clients/jobs/{job_id}").json()
        if job["state"] in ("done", "error"):
            return job
        time.sleep(0.05)
    raise AssertionError("задание не закончилось")


def _make(tmp=None):
    tmp = tmp or ck.workdir()
    return make_client(tmp, engine=FakeEngine(), kit=KitStore(tmp / "derived" / "kit.sqlite"))


def test_client_full_cycle_over_http():
    c, _svc, tmp = _make()
    gid, cid = _client_with_contract(c)
    assert (gid, cid) == ("ug1", "u01")
    for f in ck.ren_files(tmp):
        r = _upload(c, cid, f)
        assert r.status_code == 201, r.text
        assert r.json()["doc_type"] in ("contract", "program") and len(r.json()["sha256"]) == 64
    assert (tmp / "derived" / "clients" / "ug1" / "u01" / "Договор № 99.docx").is_file()
    lst = c.get("/api/clients").json()
    assert [f["name"] for f in lst[0]["contracts"][0]["files"]] == ["Договор № 99.docx",
                                                                   "Приложение 1. Программа ДМС.docx"]
    assert lst[0]["contracts"][0]["job"] is None

    r = c.post(f"/api/clients/contracts/{cid}/process")
    assert r.status_code == 202 and r.json()["contract_id"] == cid
    job = _wait(c, r.json()["id"])
    assert job["state"] == "done" and job["stage"] == "selfcheck" and job["finished_at"]
    rep = job["report"]
    assert rep["readiness"] == "ready" and rep["adapter"] == "renaissance" and rep["counts"]["clinics"] == 5
    assert rep["selfcheck"]["asked"] > 0
    assert c.get("/api/clients").json()[0]["contracts"][0]["job"]["id"] == job["id"]

    # договор клиента — в /api/groups (группа ugN с именем клиента), чат и документы работают
    groups = {g["id"]: g for g in c.get("/api/groups").json()}
    assert groups["ug1"]["name"] == "Клиент Р"
    card = groups["ug1"]["contracts"][0]
    assert card["id"] == "u01" and card["legal_entity"] == "ООО Ромашка" and card["insurer"] == "renaissance"
    assert card["programs"] == ["Альфа", "Бета"]
    chat = c.post("/api/chats", json={"context": {"contract_id": "u01", "program": "Альфа"}})
    assert chat.status_code == 201 and chat.json()["context"]["group_id"] == "ug1"
    assert {d["id"] for d in c.get("/api/documents?contract_id=u01").json()} == {"u01.contract", "u01.program"}


def test_upload_name_cleaning_format_replace_patch_delete():
    c, _svc, tmp = _make()
    _gid, cid = _client_with_contract(c)
    src = ck.ren_files(tmp)[0]
    r = _upload(c, cid, src, name="../../ evil<>:|?.docx")
    assert r.status_code == 201, r.text
    name = r.json()["name"]
    assert name == "evil_____.docx" and "/" not in name
    folder = tmp / "derived" / "clients" / "ug1" / "u01"
    assert (folder / name).is_file() and not (tmp / "derived" / "evil_____.docx").exists()
    assert r.json()["doc_type"] == "other"                                   # имя не по config/doc_types.yaml

    # то же имя — замена файла с новым sha256, файл в списке один
    other = ck.generic_docx(tmp / "x" / "y.docx", ["Другой текст"])
    r2 = _upload(c, cid, other, name=name)
    assert r2.status_code == 201 and r2.json()["sha256"] != r.json()["sha256"]
    assert [f["name"] for f in c.get("/api/clients").json()[0]["contracts"][0]["files"]] == [name]

    # формат — по сигнатуре: текст под видом docx не принимается, неизвестное расширение — тоже
    bad = c.post(f"/api/clients/contracts/{cid}/files", files={"file": ("Договор.docx", b"just text")})
    assert bad.status_code == 415 and "DOCX" in bad.json()["error"]["message"]
    assert c.post(f"/api/clients/contracts/{cid}/files", files={"file": ("a.exe", b"MZ")}).status_code == 415
    noext = c.post(f"/api/clients/contracts/{cid}/files", files={"file": ("..", b"x")})
    assert noext.status_code == 422 and noext.json()["error"]["code"] == "bad_name"
    assert c.post(f"/api/clients/contracts/{cid}/files", data={"x": "1"}).status_code == 422
    assert c.post("/api/clients/contracts/u77/files", files={"file": ("a.txt", b"t")}).status_code == 404

    # PATCH: тип и область (перенос в _group/), неизвестный тип — 422
    p = c.patch(f"/api/clients/contracts/{cid}/files/{name}", json={"doc_type": "contract"})
    assert p.status_code == 200 and p.json()["doc_type"] == "contract"
    assert c.patch(f"/api/clients/contracts/{cid}/files/{name}", json={"doc_type": "x"}).status_code == 422
    p = c.patch(f"/api/clients/contracts/{cid}/files/{name}", json={"scope": "group", "doc_type": "rules"})
    assert p.json()["scope"] == "group" and (tmp / "derived" / "clients" / "ug1" / "_group" / name).is_file()
    assert not (folder / name).exists()
    lst = c.get("/api/clients").json()[0]
    assert [f["name"] for f in lst["files"]] == [name]
    assert [(f["name"], f["scope"]) for f in lst["contracts"][0]["files"]] == [(name, "group")]
    # общие файлы группы есть, файлов договора нет — обрабатывать нечего
    r = c.post(f"/api/clients/contracts/{cid}/process")
    assert r.status_code == 422 and r.json()["error"]["code"] == "no_files"

    # DELETE
    assert c.delete(f"/api/clients/contracts/{cid}/files/{name}").status_code == 204
    assert not (tmp / "derived" / "clients" / "ug1" / "_group" / name).exists()
    assert c.delete(f"/api/clients/contracts/{cid}/files/{name}").status_code == 404


def test_client_validation_limits_and_errors_in_russian():
    from dms_core import constants

    c, _svc, _tmp = _make()
    r = c.post("/api/clients", json={"contracts": []})
    assert r.status_code == 422 and r.json()["error"]["field"] == "name" and "Заполните" in r.json()["error"]["message"]
    r = c.post("/api/clients", json={"name": "К", "contracts": [{"legal_entity": "Ю", "insurer": "ромашка"}]})
    assert r.status_code == 422 and r.json()["error"]["field"] == "contracts.0.insurer"
    r = c.post("/api/clients", json={"name": "К", "contracts": [{}]})
    assert r.status_code == 422 and r.json()["error"]["field"] == "legal_entity"
    assert c.get("/api/clients").json() == []                                 # ошибка — клиент не создан
    gid, cid = _client_with_contract(c, insurer="sogaz")
    r = c.post(f"/api/clients/{gid}/contracts", json={"legal_entity": "ООО Вторая"})
    assert r.status_code == 201 and r.json()["id"] == "u02" and r.json()["insurer_hint"] is None
    assert c.post("/api/clients/ug9/contracts", json={"legal_entity": "Ю"}).status_code == 404
    assert c.get("/api/clients/jobs/job_нет").status_code == 404
    r = c.post(f"/api/clients/contracts/{cid}/process")
    assert r.status_code == 422 and r.json()["error"]["code"] == "no_files"

    with patched(constants, "CLIENT_MAX_FILES", 1):
        assert c.post(f"/api/clients/contracts/{cid}/files", files={"file": ("a.txt", b"t")}).status_code == 201
        r = c.post(f"/api/clients/contracts/{cid}/files", files={"file": ("b.txt", b"t")})
        assert r.status_code == 422 and r.json()["error"]["code"] == "too_many_files"
        # замена того же имени лимит не превышает
        assert c.post(f"/api/clients/contracts/{cid}/files", files={"file": ("a.txt", b"t2")}).status_code == 201


def test_one_job_at_a_time_and_failed_job_keeps_db():
    c, svc, tmp = _make()
    _gid, cid = _client_with_contract(c)
    for f in ck.ren_files(tmp):
        _upload(c, cid, f)
    ok = _wait(c, c.post(f"/api/clients/contracts/{cid}/process").json()["id"])
    assert ok["state"] == "done"
    clauses = len(svc.kit.clauses(cid))

    # идёт другое задание (держим блокировку) — 409 job_running; загрузка в обрабатываемый договор — 409
    assert svc._onboard_lock.acquire(blocking=False)
    try:
        r = c.post(f"/api/clients/contracts/{cid}/process")
        assert r.status_code == 409 and r.json()["error"]["code"] == "job_running"
        assert c.post("/api/documents/import-kit").status_code in (409, 422)
    finally:
        svc._onboard_lock.release()

    # задание падает (адаптер бросает) — error, отчёт failed, прежняя версия договора в базе
    def boom(kc, root):
        raise ValueError("битый файл")
    with patched(adapters, "parse_contract", boom):
        job = _wait(c, c.post(f"/api/clients/contracts/{cid}/process").json()["id"])
    assert job["state"] == "error" and job["report"]["readiness"] == "failed" and "обработать" in job["error"]
    assert "битый файл" not in str(job)
    assert len(svc.kit.clauses(cid)) == clauses


def test_lost_job_after_restart_is_shown_as_error():
    import datetime as dt

    from dms_core.schemas import OnboardingJob

    c, svc, _tmp = _make()
    _gid, cid = _client_with_contract(c)
    svc.app.save_job(OnboardingJob(id="job_old", contract_id=cid, state="running", stage="extracted",
                                   created_at=dt.datetime.now(dt.UTC)))
    job = c.get("/api/clients/jobs/job_old").json()
    assert job["state"] == "error" and "перезапуск" in job["error"]


def test_log_has_no_client_file_names():
    records: list[logging.LogRecord] = []

    class Grab(logging.Handler):
        def emit(self, record):
            records.append(record)

    h = Grab(level=logging.DEBUG)
    loggers = [logging.getLogger(n) for n in ("dms", "dms.service", "dms.onboarding", "dms.app")]
    old = [(lg, lg.level) for lg in loggers]
    for lg in loggers:
        lg.addHandler(h)
        lg.setLevel(logging.DEBUG)
    try:
        c, _svc, tmp = _make()
        _gid, cid = _client_with_contract(c)
        for f in ck.ren_files(tmp):
            _upload(c, cid, f, name=f"Секретный {f.name}")
        _wait(c, c.post(f"/api/clients/contracts/{cid}/process").json()["id"])
        c.delete(f"/api/clients/contracts/{cid}/files/Секретный Договор № 99.docx")
    finally:
        for lg, lvl in old:
            lg.removeHandler(h)
            lg.setLevel(lvl)
    text = " ".join(r.getMessage() for r in records)
    assert "u01" in text and "Секретный" not in text and "Ромашка" not in text and "Клиника" not in text


def test_import_kit_passes_registry_for_clients():
    import tempfile
    from pathlib import Path

    seen = {}

    def ingest(root, store, registry=None, clients_root=None):
        seen.update(registry=registry, clients_root=clients_root)
        return {"contracts": {}, "groups": {}, "clients": {"u01": "ready"}, "statuses": {}, "seconds": 0}

    c, svc, tmp = make_client(engine=FakeEngine(), ingest_fn=ingest, kit_root=Path(tempfile.mkdtemp()))
    assert c.post("/api/documents/import-kit").status_code == 202
    for _ in range(100):
        st = c.get("/api/documents/import-kit").json()
        if st["state"] != "running":
            break
        time.sleep(0.05)
    assert seen["registry"] is svc.app and seen["clients_root"] == tmp / "derived" / "clients"
    assert st["report"]["clients"] == {"u01": "ready"}


def test_group_files_shared_by_all_contracts_of_group():
    """Уточнение автора к §9: общий файл группы загружается через любой договор (scope=group → _group/),
    виден в files каждого договора, правится и удаляется через любой договор; обработка берёт файлы договора и
    общие файлы группы — общая программа разбирается в каждый договор под его contract_id (ADR 0005)."""
    c, svc, tmp = _make()
    _gid, u01 = _client_with_contract(c)
    u02 = c.post("/api/clients/ug1/contracts", json={"legal_entity": "ООО Вторая"}).json()["id"]
    contract, program = ck.ren_files(tmp)
    assert _upload(c, u01, contract).status_code == 201
    assert _upload(c, u02, contract).status_code == 201
    r = _upload(c, u01, program, scope="group")
    assert r.status_code == 201 and r.json()["scope"] == "group"
    assert (tmp / "derived" / "clients" / "ug1" / "_group" / program.name).is_file()
    lst = c.get("/api/clients").json()[0]
    for k in range(2):
        assert [(f["name"], f["scope"]) for f in lst["contracts"][k]["files"]] == [
            (contract.name, "contract"), (program.name, "group")]
    # PATCH через другой договор группы
    p = c.patch(f"/api/clients/contracts/{u02}/files/{program.name}", json={"doc_type": "program"})
    assert p.status_code == 200 and p.json()["scope"] == "group"
    for cid in (u01, u02):
        job = _wait(c, c.post(f"/api/clients/contracts/{cid}/process").json()["id"])
        assert job["state"] == "done" and job["report"]["readiness"] == "ready", job
    for cid in (u01, u02):
        assert {d.id for d in svc.kit.documents(cid)} == {f"{cid}.contract", f"{cid}.program"}
        assert all(r.contract_id == cid for r in svc.kit.clinics(cid))
    assert c.delete(f"/api/clients/contracts/{u02}/files/{program.name}").status_code == 204
    assert all(f["scope"] == "contract" for f in c.get("/api/clients").json()[0]["contracts"][0]["files"])
