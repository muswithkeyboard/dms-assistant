"""Ревизия w8 (этап w8-fix), API подключения клиента: предел частей xlsx (каталог ЛПУ), zip-бомба, имя файла в байтах,
.part при ошибке записи, загрузка во время импорта, признак files_changed в GET /api/clients."""
from __future__ import annotations

import io
import os
import time
import zipfile

from api.api_kit import FakeEngine, make_client
from dms_api import service
from dms_core.store import KitStore
from fixtures import client_kit as ck
from helpers import kit_or_skip, patched


def _make():
    tmp = ck.workdir()
    return make_client(tmp, engine=FakeEngine(), kit=KitStore(tmp / "derived" / "kit.sqlite"))


def _contract(c):
    r = c.post("/api/clients", json={"name": "Клиент Ф", "contracts": [{"legal_entity": "ООО Ф"}]})
    assert r.status_code == 201, r.text
    return r.json()["id"], r.json()["contracts"][0]["id"]


def _upload(c, cid, name, data, **form):
    return c.post(f"/api/clients/contracts/{cid}/files", files={"file": (name, data)}, data=form)


def _wait(c, job_id):
    for _ in range(400):
        job = c.get(f"/api/clients/jobs/{job_id}").json()
        if job["state"] in ("done", "error"):
            return job
        time.sleep(0.05)
    raise AssertionError("задание не закончилось")


def _xlsx_zip(entries: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", "<Types/>")
        z.writestr("xl/workbook.xml", "<workbook/>")
        for k, v in entries.items():
            z.writestr(k, v)
    return buf.getvalue()


# ------------------------------------------------------------------ п. 3: каталог ЛПУ и zip-бомба

def test_real_catalog_xlsx_accepted():
    """Каталог «ЛПУ_Регионы Стандарт.xlsx» комплекта — 2181 часть: прежний предел 2000 отклонял его кодом 415."""
    hits = list(kit_or_skip().rglob("ЛПУ_Регионы*.xlsx"))
    assert hits, "в комплекте нет каталога ЛПУ"
    data = hits[0].read_bytes()
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        assert len(z.infolist()) > 2000
    assert service.sniff_format("ЛПУ_Регионы Стандарт.xlsx", data) == "xlsx"


def test_zip_bomb_and_too_many_parts_are_413_not_415():
    many = _xlsx_zip({f"xl/worksheets/s{i}.xml": b"" for i in range(service.MAX_ZIP_ENTRIES)})
    big = _xlsx_zip({"xl/worksheets/sheet1.xml": b"\0" * (2 * 2**20)})
    for data, word in ((many, "частей"), (big, "распакованном")):
        with patched(service, "MAX_UNPACKED", 2**20):
            try:
                service.sniff_format("a.xlsx", data)
                raise AssertionError("ожидалась ошибка размера")
            except service.ServiceError as e:
                assert (e.status, e.code) == (413, "payload_too_large") and word in e.message
    assert service.sniff_format("a.xlsx", _xlsx_zip({"xl/worksheets/sheet1.xml": b"<x/>"})) == "xlsx"
    c, _svc, _tmp = _make()
    _gid, cid = _contract(c)
    with patched(service, "MAX_UNPACKED", 2**20):
        r = _upload(c, cid, "Перечень.xlsx", big)
    assert r.status_code == 413 and "распакованном" in r.json()["error"]["message"]


# ------------------------------------------------------------------ п. 4: имя в байтах и .part

def test_long_cyrillic_name_trimmed_by_utf8_bytes():
    name = service.clean_filename("Я" * 145 + ".docx")
    assert name.endswith(".docx") and len(name.encode("utf-8")) <= service.MAX_FILENAME_BYTES
    assert set(name[:-5]) == {"Я"} and len(name) - 5 == (service.MAX_FILENAME_BYTES - 5) // 2
    odd = service.clean_filename("a" + "Я" * 145 + ".txt")          # граница посреди двухбайтовой буквы
    odd.encode("utf-8").decode("utf-8")
    assert odd.endswith(".txt") and len(odd.encode("utf-8")) <= service.MAX_FILENAME_BYTES
    c, _svc, tmp = _make()
    _gid, cid = _contract(c)
    r = _upload(c, cid, "Я" * 145 + ".txt", "1. Пункт договора".encode())
    assert r.status_code == 201, r.text
    assert (tmp / "derived" / "clients" / "ug1" / cid / r.json()["name"]).is_file()


def test_part_file_removed_on_write_error():
    c, _svc, tmp = _make()
    _gid, cid = _contract(c)
    folder = tmp / "derived" / "clients" / "ug1" / cid

    def boom(*a, **kw):
        raise OSError("диск заполнен")
    with patched(os, "replace", boom):
        r = _upload(c, cid, "Договор.txt", "1. Пункт".encode())
    assert r.status_code == 500 and r.json()["error"]["code"] == "write_failed"
    assert not list(folder.glob("*.part")) and not list(folder.glob(".*.part"))
    assert c.get("/api/clients").json()[0]["contracts"][0]["files"] == []


# ------------------------------------------------------------------ п. 6: изменение файлов во время импорта

def test_files_locked_while_import_runs():
    c, svc, _tmp = _make()
    _gid, cid = _contract(c)
    assert _upload(c, cid, "Договор.txt", "1. Пункт".encode()).status_code == 201
    svc._import_lock.acquire()
    try:
        for r in (_upload(c, cid, "Ещё.txt", b"2. x"), c.delete(f"/api/clients/contracts/{cid}/files/Договор.txt"),
                  c.patch(f"/api/clients/contracts/{cid}/files/Договор.txt", json={"doc_type": "memo"})):
            assert r.status_code == 409 and r.json()["error"]["code"] == "import_running", r.text
    finally:
        svc._import_lock.release()
    assert c.delete(f"/api/clients/contracts/{cid}/files/Договор.txt").status_code == 204


# ------------------------------------------------------------------ п. 7: files_changed

def test_files_changed_flag_and_contract_stays_in_search():
    c, _svc, _tmp = _make()
    gid, cid = _contract(c)
    body = "ДОГОВОР № 3\n1. Предмет договора\n1.1. Страховщик оплачивает медицинские услуги.\n".encode()
    assert _upload(c, cid, "Договор 3.txt", body, doc_type="contract").status_code == 201
    item = c.get("/api/clients").json()[0]["contracts"][0]
    assert item["files_changed"] is False and item["in_search"] is False           # ещё не обрабатывался
    job = _wait(c, c.post(f"/api/clients/contracts/{cid}/process").json()["id"])
    assert job["state"] == "done" and job["report"]["readiness"] == "limited"
    item = c.get("/api/clients").json()[0]["contracts"][0]
    assert item["files_changed"] is False and item["in_search"] is True
    assert c.delete(f"/api/clients/contracts/{cid}/files/Договор 3.txt").status_code == 204
    item = c.get("/api/clients").json()[0]["contracts"][0]
    assert item["files_changed"] is True and item["in_search"] is True and item["files"] == []
    groups = {g["id"]: g for g in c.get("/api/groups").json()}
    assert [x["id"] for x in groups[gid]["contracts"]] == [cid]                    # из поиска молча не удалён
    # тот же файл обратно — совпадает с обработанным
    assert _upload(c, cid, "Договор 3.txt", body, doc_type="contract").status_code == 201
    assert c.get("/api/clients").json()[0]["contracts"][0]["files_changed"] is False
