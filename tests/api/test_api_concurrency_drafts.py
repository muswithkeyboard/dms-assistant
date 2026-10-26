"""Ревизия w3: правка черновика (запрос открыт, фильтр обещаний), блокировка на запрос (нет потери обновлений),
просмотр пунктов из чата — только документы договора(ов) контекста чата."""
from __future__ import annotations

import threading
import time

from api.api_kit import FakeEngine, fake_add_clinic, make_client, new_request


def _with_draft(**kw):
    c, svc, _tmp = make_client(engine=FakeEngine(), **kw)
    chat_id, rid = new_request(c)
    assert c.post(f"/api/requests/{rid}/messages", json={"text": "Клиника «Улыбка» есть?"}).status_code == 200
    r = c.post(f"/api/requests/{rid}/automation/add-clinic", json={"clinic": "Улыбка"})
    assert r.status_code == 200, r.text
    return c, svc, chat_id, rid, r.json()["drafts"][0]["id"]


def test_patch_draft_requires_open_request():
    c, _, _, rid, did = _with_draft()
    assert c.post(f"/api/requests/{rid}/close").status_code == 200
    r = c.patch(f"/api/drafts/{did}", json={"body": "Новый текст"})
    assert r.status_code == 409 and "Запрос закрыт" in r.json()["error"]["message"]
    assert c.post(f"/api/requests/{rid}/reopen").status_code == 200
    assert c.patch(f"/api/drafts/{did}", json={"body": "Новый текст"}).status_code == 200


def test_patch_draft_with_promise_is_422_and_not_saved():
    c, _, _, rid, did = _with_draft()
    before = c.get(f"/api/requests/{rid}").json()["drafts"][0]
    r = c.patch(f"/api/drafts/{did}", json={"body": "Клиника будет добавлена в течение 3 дней, гарантируем."})
    assert r.status_code == 422, r.text
    err = r.json()["error"]
    assert err["code"] == "promise" and err["field"] == "body"
    for phrase in ("«будет добавлена»", "«в течение 3 дней»", "«гарантируем»"):
        assert phrase in err["message"], err["message"]
    # обещание в теме — тоже 422; черновик не изменился
    assert c.patch(f"/api/drafts/{did}", json={"subject": "Оплатим завтра"}).status_code == 422
    after = c.get(f"/api/requests/{rid}").json()["drafts"][0]
    assert after["body"] == before["body"] and after["subject"] == before["subject"] and not after["edited_at"]
    # переписанный без обещаний текст сохраняется
    ok = c.patch(f"/api/drafts/{did}", json={"body": "Уточним у страховой компании условия и сообщим."})
    assert ok.status_code == 200 and ok.json()["edited_at"]


def test_parallel_step_and_rate_do_not_lose_updates():
    """Шаг автомата долго считается; в это время менеджер ставит оценку. Без блокировки на запрос шаг сохранил бы
    прочитанную до оценки карточку и оценка пропала бы (store.save_request пишет карточку целиком)."""
    mod = fake_add_clinic()
    slow_step = mod.step
    entered = threading.Event()

    def step(request, action, **kw):
        entered.set()
        time.sleep(0.3)
        return slow_step(request, action, **kw)

    mod.step = step
    _c, svc, _, rid, _ = _with_draft(add_clinic=mod)
    errors: list[BaseException] = []

    def run_step() -> None:
        try:
            svc.automation_step(rid, {"action": "hr_wants_add"})
        except BaseException as e:  # noqa: BLE001 — передать сбой потока в тест
            errors.append(e)

    def run_rate() -> None:
        entered.wait(2)
        try:
            svc.rate(rid, {"rating": 4, "comment": "ок"})
        except BaseException as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=run_step), threading.Thread(target=run_rate)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(5)
    assert not errors, errors
    saved = svc.app.request(rid)
    assert saved.automation and saved.automation.state.value == "hr_wants_add"
    assert saved.rating == 4 and saved.rating_comment == "ок"
    assert len(saved.drafts) == 2


def test_parallel_patch_and_step_keep_both():
    """Правка черновика во время шага автомата: шаг перечитывает запрос под блокировкой и не затирает правку."""
    mod = fake_add_clinic()
    slow_step = mod.step
    entered = threading.Event()

    def step(request, action, **kw):
        entered.set()
        time.sleep(0.3)
        return slow_step(request, action, **kw)

    mod.step = step
    _c, svc, _, rid, did = _with_draft(add_clinic=mod)
    t1 = threading.Thread(target=lambda: svc.automation_step(rid, {"action": "hr_wants_add"}))
    t2 = threading.Thread(target=lambda: (entered.wait(2), svc.edit_draft(did, {"body": "Правка менеджера"})))
    t1.start()
    t2.start()
    t1.join(5)
    t2.join(5)
    saved = svc.app.request(rid)
    assert saved.automation and saved.automation.state.value == "hr_wants_add" and len(saved.drafts) == 2
    assert next(d for d in saved.drafts if d.id == did).body == "Правка менеджера"


def test_clause_with_chat_id_only_from_chat_contracts():
    c, _, _ = make_client(engine=FakeEngine())
    chat1 = c.post("/api/chats", json={"context": {"contract_id": "c01"}}).json()["id"]
    gk = c.post("/api/chats", json={"context": {"group_id": "gk9"}}).json()["id"]
    url = "/api/documents/{}/clause?number=4.1&chat_id={}"
    assert c.get(url.format("c01.program", chat1)).status_code == 200
    r = c.get(url.format("c02.program", chat1))
    assert r.status_code == 404 and "не относится к договорам этого чата" in r.json()["error"]["message"]
    # контекст ГК — документы всех её договоров
    assert c.get(url.format("c02.program", gk)).status_code == 200
    # список пунктов — та же проверка
    assert c.get(f"/api/documents/c01.program/clauses?chat_id={chat1}").status_code == 200
    assert c.get(f"/api/documents/c02.program/clauses?chat_id={chat1}").status_code == 404
    # неизвестный чат — 404; без chat_id — как раньше (страница документов)
    assert c.get(url.format("c01.program", "ch_нет")).status_code == 404
    assert c.get("/api/documents/c02.program/clause?number=4.1").status_code == 200
