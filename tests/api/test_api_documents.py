"""Контексты, документы, источники, загрузка файла (сигнатуры, статусы ТЗ п. 7.1), импорт комплекта в фоне."""
from __future__ import annotations

import io
import time
import zipfile

from api.api_kit import FakeEngine, make_client


def test_groups_contract_documents_and_clause():
    c, _, _ = make_client(engine=FakeEngine())
    groups = c.get("/api/groups").json()
    assert groups[0]["id"] == "gk9" and [x["id"] for x in groups[0]["contracts"]] == ["c01", "c02"]
    assert groups[0]["contracts"][0]["programs"] == ["Стандарт"]

    card = c.get("/api/contracts/c01").json()
    assert card["legal_entity"] == "Компания 1" and {d["id"] for d in card["documents"]} >= {"c01.contract"}
    assert c.get("/api/contracts/c99").status_code == 404

    docs = c.get("/api/documents?contract_id=c02").json()
    assert all(d["contract_id"] == "c02" for d in docs)
    tree = {d["id"] for d in c.get("/api/documents?group_id=gk9").json()}
    assert "gk9.rules" in tree and "c01.program" in tree and "c02.program" in tree
    assert c.get("/api/documents").status_code == 422
    assert c.get("/api/documents/c01.program").json()["doc_type"] == "program"

    cl = c.get("/api/documents/c01.program/clause?number=4.1").json()["clause"]
    assert cl["contract_id"] == "c01" and "Франшиза" in cl["text"]
    by_ref = c.get("/api/documents/c01.program/clause", params={"ref": cl["id"]}).json()["clause"]
    assert by_ref["id"] == cl["id"]
    row = c.get("/api/documents/c01.clinics/clause?ref=t0:r1").json()["clinic"]
    assert row["contract_id"] == "c01" and row["care_type"] == "Стоматологическая помощь"
    # пункт чужого договора по ссылке из документа c01 не находится (ADR 0005)
    c02 = c.get("/api/documents/c02.program/clause?number=4.1").json()["clause"]
    assert c.get("/api/documents/c01.program/clause", params={"ref": c02["id"]}).status_code == 404
    assert "не найден" in c.get("/api/documents/c01.program/clause?number=99").json()["error"]["message"]


def test_chat_context_validation_in_russian():
    c, _, _ = make_client(engine=FakeEngine())
    r = c.post("/api/chats", json={})
    assert r.status_code == 422 and r.json()["error"]["field"] == "context"
    r = c.post("/api/chats", json={"context": {"contract_id": "c99"}})
    assert r.status_code == 422 and "Договор не найден" in r.json()["error"]["message"]
    r = c.post("/api/chats", json={"context": {"contract_id": "c01", "program": "Нет такой"}})
    assert r.json()["error"]["field"] == "program"
    r = c.post("/api/chats", json={"context": {"group_id": "gk9", "date": "31.12.2026"}})
    assert r.status_code == 422 and "ГГГГ-ММ-ДД" in r.json()["error"]["message"]
    g = c.post("/api/chats", json={"context": {"group_id": "gk9", "date": "2026-10-19"}}).json()
    assert g["title"].startswith("ГК 9") and "19.10.2026" in g["title"]
    assert [x["id"] for x in c.get("/api/chats").json()] == [g["id"]]
    assert c.get("/api/chats/нет").status_code == 404
    r = c.post("/api/chats", content=b"{not json", headers={"content-type": "application/json"})
    assert r.status_code == 422 and "JSON" in r.json()["error"]["message"]


def _docx_bytes(document_xml: str = "<w:document/>") -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("[Content_Types].xml", "<Types/>")
        z.writestr("word/document.xml", document_xml)
    return buf.getvalue()


def test_upload_signature_metadata_and_statuses():
    c, _svc, tmp = make_client(engine=FakeEngine())
    ok = c.post("/api/documents", files={"file": ("Памятка.txt", "Памятка застрахованному: телефон пульта.".encode())},
                data={"contract_id": "c01", "uploaded_by": "Оператор"})
    assert ok.status_code == 201, ok.text
    d = ok.json()
    assert d["status"] == "checked" and d["original_format"] == "txt" and d["uploaded_by"] == "Оператор"
    assert d["group_id"] == "gk9" and d["uploaded_at"] and len(d["sha256"]) == 64
    assert (tmp / d["source_path"]).is_file()                  # файл — в data/derived/uploads (вне git)
    assert "Памятка" not in str(d["warnings"])                  # текста документа в ответе нет
    assert d["id"] in {x["id"] for x in c.get("/api/documents?contract_id=c01").json()}

    # расширение не совпадает с содержимым → 415
    fake = c.post("/api/documents", files={"file": ("Договор.docx", b"just text")}, data={"contract_id": "c01"})
    assert fake.status_code == 415 and "DOCX" in fake.json()["error"]["message"]
    assert c.post("/api/documents", files={"file": ("a.pdf", b"PK\x03\x04")},
                  data={"contract_id": "c01"}).status_code == 415
    assert c.post("/api/documents", files={"file": ("a.exe", b"MZ")}, data={"contract_id": "c01"}).status_code == 415
    # zip без части word/document.xml — не docx
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("[Content_Types].xml", "<Types/>")
    assert c.post("/api/documents", files={"file": ("x.docx", buf.getvalue())},
                  data={"contract_id": "c01"}).status_code == 415
    # docx с нужными частями, но битым XML — принят, статус «Ошибка» с причиной по-русски
    broken = c.post("/api/documents", files={"file": ("Программа.docx", _docx_bytes("<oops"))},
                    data={"contract_id": "c01"}).json()
    assert broken["status"] == "error" and broken["warnings"]
    # без договора и группы — 422
    r = c.post("/api/documents", files={"file": ("a.txt", b"text")})
    assert r.status_code == 422 and r.json()["error"]["field"] == "contract_id"


def test_upload_too_large_is_413():
    from dms_api.settings import Settings

    c, _, _ = make_client(engine=FakeEngine(), settings=Settings(_env_file=None, max_upload_mb=1))
    big = b"a" * (1024 * 1024 + 10)
    r = c.post("/api/documents", files={"file": ("big.txt", big)}, data={"contract_id": "c01"})
    assert r.status_code == 413 and "размер" in r.json()["error"]["message"]


def test_import_kit_runs_in_background_without_texts():
    calls = []

    def ingest(root, store):
        calls.append(root)
        time.sleep(0.2)
        return {"contracts": {"c01": {"group_id": "gk9", "status": "ok", "clauses": 7, "documents": [{"path": "x"}],
                                      "warnings": ["текст предупреждения"]}},
                "groups": {"gk9": {"documents": [{}]}}, "statuses": {"contracts": {"ready": 3}}, "seconds": 0.2}

    import tempfile
    from pathlib import Path

    root = Path(tempfile.mkdtemp())
    c, _svc, _ = make_client(engine=FakeEngine(), ingest_fn=ingest, kit_root=root)
    r = c.post("/api/documents/import-kit")
    assert r.status_code == 202 and r.json()["state"] == "running"
    assert c.post("/api/documents/import-kit").status_code == 409          # второй импорт — нельзя
    for _ in range(100):
        st = c.get("/api/documents/import-kit").json()
        if st["state"] != "running":
            break
        time.sleep(0.05)
    assert st["state"] == "done" and calls == [root]
    rep = st["report"]["contracts"]["c01"]
    assert rep == {"group_id": "gk9", "status": "ok", "clauses": 7, "documents": 1, "warnings": 1}
    assert "текст предупреждения" not in str(st)
