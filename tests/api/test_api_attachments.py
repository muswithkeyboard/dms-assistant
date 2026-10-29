"""Документ в чат (ADR 0016): загрузка, сообщение «загружен», ответ по вложению, изоляция по чату, настройка
«также в документы договора» и участие документа в поиске договора."""
from __future__ import annotations

import io

from api.api_kit import FakeEngine, make_client, new_request

TXT = ("1. Амбулаторная помощь\n"
       "1.1. Приём врачей-специалистов: терапевта и кардиолога по направлению врача.\n"
       "2. Нетрадиционная медицина\n"
       "2.1. Сеансы иглорефлексотерапии оплачиваются в пределах десяти процедур за год.\n").encode()


def _attach(c, chat_id, data=TXT, name="Памятка по иглам.txt", **fields):
    return c.post(f"/api/chats/{chat_id}/attachments", files={"file": (name, io.BytesIO(data), "text/plain")},
                  data=fields)


def test_attach_intro_and_answer_by_attachment():
    engine = FakeEngine()
    c, _, _ = make_client(engine=engine)
    chat_id, rid = new_request(c)
    r = _attach(c, chat_id, request_id=rid)
    assert r.status_code == 201, r.text
    out = r.json()
    att = out["attachment"]
    assert att["id"].startswith("att_") and att["active"] and att["clauses"] == 4 and att["added_to"] is None
    assert out["request_id"] == rid
    intro = out["message"]["answer"]["reply"]
    assert "загружен" in intro["lead"] and intro["options"][0] == "Что сказано в разделе «Амбулаторная помощь»?"

    r = c.post(f"/api/requests/{rid}/messages", json={"text": "Сколько сеансов иглорефлексотерапии?"})
    assert r.status_code == 200, r.text
    a = r.json()["answer"]
    assert not engine.calls                              # мимо движка договора
    assert a["label"] is None and a["reply"]["points"]
    ref = a["reply"]["points"][0]["refs"][0]
    cit = a["statements"][0]["citations"][0]
    assert cit["verified"] and cit["doc_id"] == att["id"] and "иглорефлексотерапии" in cit["quote"]
    # пункт вложения открывается по ссылке — только из своего чата
    cl = c.get(f"/api/documents/{att['id']}/clause", params={"ref": ref, "chat_id": chat_id})
    assert cl.status_code == 200 and "десяти" in cl.json()["clause"]["text"]
    assert c.get(f"/api/documents/{att['id']}/clause", params={"ref": ref}).status_code == 404
    other = c.post("/api/chats", json={"context": {"contract_id": "c02"}}).json()["id"]
    assert c.get(f"/api/documents/{att['id']}/clause", params={"ref": ref, "chat_id": other}).status_code == 404
    assert c.put(f"/api/chats/{other}/attachments/active", json={"attachment_id": att["id"]}).status_code == 404

    # ✕ на плашке: снова по договору (движок), вложение остаётся в списке
    r = c.put(f"/api/chats/{chat_id}/attachments/active", json={"attachment_id": None})
    assert r.json()["active"] is None and len(r.json()["items"]) == 1
    c.post(f"/api/requests/{rid}/messages", json={"text": "Есть ли франшиза?"})
    assert len(engine.calls) == 1
    assert c.get(f"/api/chats/{other}/attachments").json()["items"] == []


def test_attach_rejects_bad_files_in_russian():
    c, _, _ = make_client(engine=FakeEngine())
    chat_id, _ = new_request(c)
    r = _attach(c, chat_id, data=b"MZ\x00\x00", name="virus.exe")
    assert r.status_code == 415 and "DOCX" in r.json()["error"]["message"]
    r = _attach(c, chat_id, data=b"   \n\n", name="empty.txt")
    assert r.status_code == 422 and "текста" in r.json()["error"]["message"]
    r = _attach(c, chat_id, data=b"PK\x03\x04broken", name="x.docx")
    assert r.status_code == 415
    assert c.get(f"/api/chats/{chat_id}/attachments").json()["items"] == []


def test_settings_validation():
    c, _, _ = make_client(engine=FakeEngine())
    assert c.get("/api/settings").json() == {"chat_upload_target": "chat"}
    assert c.put("/api/settings", json={"chat_upload_target": "everywhere"}).status_code == 422
    assert c.put("/api/settings", json={"x": 1}).status_code == 422
    assert c.put("/api/settings", json={"chat_upload_target": "contract"}).json()["chat_upload_target"] == "contract"
    assert c.get("/settings").status_code == 200


def test_add_to_contract_documents_and_search():
    c, svc, _ = make_client(engine=FakeEngine())
    c.put("/api/settings", json={"chat_upload_target": "contract"})
    rev0 = svc.kit.revision("c01")
    chat_id, rid = new_request(c)
    r = _attach(c, chat_id, request_id=rid, doc_type="memo")
    assert r.status_code == 201, r.text
    att = r.json()["attachment"]
    assert att["added_to"] == "c01" and att["kit_doc_id"].startswith("c01.chat_")
    assert "добавлен в документы договора" in r.json()["message"]["answer"]["reply"]["caution"]
    docs = {d["id"]: d for d in c.get("/api/documents?contract_id=c01").json()}
    kd = docs[att["kit_doc_id"]]
    assert kd["uploaded_by"] == "chat" and kd["doc_type"] == "memo" and "Повторный импорт" in kd["warnings"][0]
    assert att["kit_doc_id"] not in {d["id"] for d in c.get("/api/documents?contract_id=c02").json()}
    assert svc.kit.revision("c01") != rev0
    from dms_core.index.search import search_clauses

    hits = search_clauses(svc.kit, "c01", "иглорефлексотерапия", k=3)
    assert hits and hits[0][0].doc_id == att["kit_doc_id"]
    assert not search_clauses(svc.kit, "c02", "иглорефлексотерапия", k=3)        # ADR 0005
    # повторная загрузка того же файла — то же вложение, документ договора не дублируется
    r2 = _attach(c, chat_id, request_id=rid, doc_type="memo")
    assert r2.json()["attachment"]["id"] == att["id"]
    assert sum(1 for d in c.get("/api/documents?contract_id=c01").json() if d["uploaded_by"] == "chat") == 1


def test_group_chat_needs_contract_choice():
    c, _, _ = make_client(engine=FakeEngine())
    c.put("/api/settings", json={"chat_upload_target": "contract"})
    chat_id, rid = new_request(c, {"group_id": "gk9"})
    r = _attach(c, chat_id, request_id=rid)
    assert r.status_code == 422 and r.json()["error"]["field"] == "contract_id"
    assert _attach(c, chat_id, request_id=rid, contract_id="c77").status_code == 422
    r = _attach(c, chat_id, request_id=rid, contract_id="c02")
    assert r.status_code == 201 and r.json()["attachment"]["added_to"] == "c02"


def test_delete_attachment_only_own_chat_and_file_removed():
    engine = FakeEngine()
    c, svc, _ = make_client(engine=engine)
    chat_id, rid = new_request(c)
    att = _attach(c, chat_id, request_id=rid).json()["attachment"]
    intro = c.get(f"/api/chats/{chat_id}/attachments").json()
    assert intro["active"] == att["id"]
    files = list((svc.data_dir / "app" / "attachments").glob(f"{att['id']}.*"))
    assert files
    other = c.post("/api/chats", json={"context": {"contract_id": "c02"}}).json()["id"]
    assert c.delete(f"/api/chats/{other}/attachments/{att['id']}").status_code == 404     # чужой чат
    assert c.delete(f"/api/chats/{chat_id}/attachments/bad").status_code == 422
    r = c.delete(f"/api/chats/{chat_id}/attachments/{att['id']}")
    assert r.status_code == 200 and r.json()["items"] == [] and r.json()["active"] is None
    assert not any(f.exists() for f in files)
    # после удаления вопросы снова по договору чата
    c.post(f"/api/requests/{rid}/messages", json={"text": "Есть ли франшиза?"})
    assert len(engine.calls) == 1
    assert c.delete(f"/api/chats/{chat_id}/attachments/{att['id']}").status_code == 404


def test_attach_intro_is_service_message():
    c, _, _ = make_client(engine=FakeEngine())
    chat_id, rid = new_request(c)
    intro = _attach(c, chat_id, request_id=rid).json()["message"]["answer"]["reply"]
    assert intro["service"] is True


def test_delete_chat_removes_requests_attachments_and_files():
    """Удаление чата: обращения, сообщения, вложения и их файлы — целиком; другие чаты не задеты; второй раз — 404;
    пока в чате считается ответ — 409."""
    c, svc, tmp = make_client(engine=FakeEngine())
    chat_id, rid = new_request(c)
    other, other_rid = new_request(c)
    assert c.post(f"/api/requests/{rid}/messages", json={"text": "Входит ли стоматология?"}).status_code == 200
    aid = _attach(c, chat_id).json()["attachment"]["id"]
    folder = tmp / "app" / "attachments"
    assert list(folder.glob(f"{aid}.*"))
    # обращение занято (идёт расчёт) — удалять нельзя
    svc.prepare_message(rid, {"text": "ещё"})
    assert c.delete(f"/api/chats/{chat_id}").status_code == 409
    svc.release_busy(rid)
    r = c.delete(f"/api/chats/{chat_id}")
    assert r.status_code == 200 and r.json() == {"deleted": chat_id}
    assert c.get(f"/api/chats/{chat_id}").status_code == 404
    assert c.get(f"/api/requests/{rid}").status_code == 404
    assert not list(folder.glob(f"{aid}.*"))
    assert c.delete(f"/api/chats/{chat_id}").status_code == 404
    assert c.get(f"/api/chats/{other}").status_code == 200 and c.get(f"/api/requests/{other_rid}").status_code == 200
    assert chat_id not in {x["id"] for x in c.get("/api/chats").json()}
