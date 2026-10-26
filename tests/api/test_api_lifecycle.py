"""Жизненный цикл через HTTP: чат → запрос → сообщение → автомат → черновик → закрыть → закрытые → оценка;
история после «перезапуска» (новый AppStore на том же файле). Движок и автомат — заглушки (CONTRACTS §8)."""
from __future__ import annotations

import json

from api.api_kit import FakeEngine, make_client, new_request
from dms_api.store import AppStore
from dms_core.llm.base import LlmError
from dms_core.schemas import ChatContext


def test_full_lifecycle_and_restart():
    engine = FakeEngine()
    c, _svc, tmp = make_client(engine=engine, llm=object())
    chat_id, rid = new_request(c)
    chat = c.get(f"/api/chats/{chat_id}").json()
    assert chat["context"]["contract_id"] == "c01" and chat["context"]["group_id"] == "gk9"
    assert "Компания 1" in chat["title"] and len(chat["requests"]) == 1

    # сообщение: движок получает контекст ЧАТА (ADR 0006), ответ сохраняется вместе с вопросом
    r = c.post(f"/api/requests/{rid}/messages", json={"text": "Можно ли добавить клинику «Улыбка»?"})
    assert r.status_code == 200, r.text
    assert r.json()["answer"]["mode"] == "live" and r.json()["message"]["role"] == "assistant"
    _q, ctx, _llm = engine.calls[0]
    assert ctx == ChatContext(group_id="gk9", contract_id="c01")
    req = c.get(f"/api/requests/{rid}").json()
    assert [m["role"] for m in req["messages"]] == ["user", "assistant"]
    assert req["scenario"] == "sc02_add_clinic"

    # автомат: запуск (повтор — без дублей), шаги по действию менеджера, недопустимый шаг — 422 по-русски
    r = c.post(f"/api/requests/{rid}/automation/add-clinic", json={"clinic": "Улыбка"})
    assert r.status_code == 200, r.text
    assert r.json()["automation"]["state"] == "not_in_program" and len(r.json()["drafts"]) == 1
    again = c.post(f"/api/requests/{rid}/automation/add-clinic", json={"clinic": "Улыбка"}).json()
    assert len(again["drafts"]) == 1
    bad = c.post(f"/api/requests/{rid}/automation/step", json={"action": "insurer_free"})
    assert bad.status_code == 422 and "недоступно" in bad.json()["error"]["message"]
    for action in ("hr_wants_add", "insurer_request_ready"):
        r = c.post(f"/api/requests/{rid}/automation/step", json={"action": action})
        assert r.status_code == 200, r.text
    r = c.post(f"/api/requests/{rid}/automation/step",
               json={"action": "insurer_paid", "insurer_reply": "Добавление платное"})
    assert r.json()["automation"]["state"] == "insurer_paid"
    assert r.json()["automation"]["history"][-1]["insurer_reply"] == "Добавление платное"

    # правка черновика: edited_at, сохранено; письма не отправляются (auto_send всегда false)
    draft_id = r.json()["drafts"][0]["id"]
    d = c.patch(f"/api/drafts/{draft_id}", json={"subject": "Новая тема", "body": "Новый текст"}).json()
    assert d["subject"] == "Новая тема" and d["edited_at"] and d["auto_send"] is False
    assert c.patch("/api/drafts/нет", json={"body": "x"}).status_code == 404

    # закрыть → нет среди открытых, есть в «показать закрытые»; в закрытый запрос писать нельзя
    assert c.post(f"/api/requests/{rid}/close").json()["status"] == "closed"
    assert c.get(f"/api/chats/{chat_id}/requests?status=open").json() == []
    closed = c.get(f"/api/chats/{chat_id}/requests?status=closed").json()
    assert [x["id"] for x in closed] == [rid]
    r = c.post(f"/api/requests/{rid}/messages", json={"text": "ещё вопрос"})
    assert r.status_code == 409 and "Верните" in r.json()["error"]["message"]

    # оценка
    rated = c.post(f"/api/requests/{rid}/rate", json={"rating": 5, "comment": "спасибо"}).json()
    assert rated["rating"] == 5 and rated["rating_comment"] == "спасибо"
    assert c.post(f"/api/requests/{rid}/rate", json={"rating": 7}).status_code == 422
    assert c.post(f"/api/requests/{rid}/reopen").json()["status"] == "open"

    # «перезапуск»: новый AppStore на том же файле — история, автомат, черновики и оценка на месте
    store2 = AppStore(tmp / "app" / "app.sqlite")
    saved = store2.request(rid)
    assert [m.role for m in saved.messages] == ["user", "assistant"]
    assert saved.automation.state.value == "insurer_paid" and saved.rating == 5
    assert any(d.subject == "Новая тема" for d in saved.drafts)
    assert [ch.id for ch in store2.chats()] == [chat_id]
    c2, _, _ = make_client(tmp, engine=engine)
    assert c2.get(f"/api/requests/{rid}").json()["messages"][1]["answer"]["summary"]


def test_sse_stream_has_stages_and_answer():
    c, _, _ = make_client(engine=FakeEngine(), llm=object())
    _, rid = new_request(c)
    r = c.post(f"/api/requests/{rid}/messages", json={"text": "Какая франшиза?"},
               headers={"Accept": "text/event-stream"})
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
    events = [b.split("\n") for b in r.text.strip().split("\n\n")]
    names = [e[0].removeprefix("event: ") for e in events if e[0].startswith("event:")]
    assert names[0] == "stage" and names[-1] == "answer"
    stages = [json.loads(e[1].removeprefix("data: "))["stage"] for e in events if e[0] == "event: stage"]
    assert stages[:2] == ["received", "route"] and "compose" in stages
    final = json.loads(events[-1][1].removeprefix("data: "))
    assert final["answer"]["summary"] and final["message"]["role"] == "assistant"


def test_model_failure_falls_back_to_extractive_in_russian():
    engine = FakeEngine(fail_with_llm=LlmError("сеть недоступна"))
    c, _, _ = make_client(engine=engine, llm=object())
    _, rid = new_request(c)
    r = c.post(f"/api/requests/{rid}/messages", json={"text": "Какая франшиза?"})
    assert r.status_code == 200, r.text
    ans = r.json()["answer"]
    assert ans["mode"] == "extractive" and "Модель недоступна" in ans["limitations"][0]
    assert engine.calls[-1][2] is None            # повтор — без модели


def test_real_engine_without_model_is_guided():
    """Без подмены движка: engine.answer на синтетическом комплекте, модели нет → guided (ADR 0009) по договору чата."""
    c, _, _ = make_client(engine=None, llm=None)
    _, rid = new_request(c)
    r = c.post(f"/api/requests/{rid}/messages", json={"text": "профессиональная гигиена полости рта"})
    assert r.status_code == 200, r.text
    ans = r.json()["answer"]
    assert ans["mode"] in ("guided", "extractive")
    cids = {cit["contract_id"] for st in ans["statements"] for cit in st["citations"]}
    assert cids <= {"c01"}                         # ничего из соседнего договора (ADR 0005)


def test_demo_saved_only_for_matching_example_and_context():
    engine = FakeEngine()
    c, _svc, tmp = make_client(engine=engine, llm=None)
    gold = tmp / "derived" / "gold"
    gold.mkdir(parents=True)
    ex = {"id": "K10", "question": "Какой следующий взнос?", "context": {"group_id": "gk9", "contract_id": "c01"},
          "expected": {"key_facts": ["секрет эталона"]}, "reference_answer": "эталон"}
    (gold / "k10_k19.jsonl").write_text(json.dumps(ex, ensure_ascii=False) + "\n", encoding="utf-8")
    saved = engine("q", ChatContext(group_id="gk9", contract_id="c01"))
    saved.summary = "Сохранённый ответ"
    (tmp / "app" / "demo_saved").mkdir(parents=True)
    (tmp / "app" / "demo_saved" / "K10.json").write_text(saved.model_dump_json(), encoding="utf-8")

    # примеры: без эталонов
    exs = c.get("/api/examples").json()
    assert exs == [{"id": "K10", "title": "Какой следующий взнос?", "question": "Какой следующий взнос?",
                    "context": {"group_id": "gk9", "contract_id": "c01", "program": None, "date": None}}]
    assert "эталон" not in json.dumps(exs, ensure_ascii=False)

    _, rid = new_request(c)
    ans = c.post(f"/api/requests/{rid}/messages", json={"text": "какой следующий взнос"}).json()["answer"]
    assert ans["mode"] == "demo_saved" and ans["summary"] == "Сохранённый ответ"
    # тот же вопрос в чате другого договора — не демо (контекст не переносится)
    _, rid2 = new_request(c, {"contract_id": "c02"})
    ans2 = c.post(f"/api/requests/{rid2}/messages", json={"text": "Какой следующий взнос?"}).json()["answer"]
    assert ans2["mode"] == "extractive"


def test_examples_empty_without_file():
    c, _, _ = make_client(engine=FakeEngine())
    assert c.get("/api/examples").json() == []


def test_engine_slots_are_saved_to_request():
    # Слоты маршрутизации движка (trace["slots"]) копятся в запросе: из них автомат берёт клинику и вид помощи
    engine = FakeEngine()
    base = engine.__call__

    def with_trace(question, context, *, llm=None, store=None, trace=None):
        if trace is not None:
            trace["slots"] = {"clinic": "Улыбка", "care_type": "Стоматология"}
        return base(question, context, llm=llm, store=store)

    c, _svc, _tmp = make_client(engine=with_trace, llm=object())
    _chat, rid = new_request(c)
    assert c.post(f"/api/requests/{rid}/messages", json={"text": "Добавить клинику «Улыбка»?"}).status_code == 200
    assert c.get(f"/api/requests/{rid}").json()["slots"] == {"clinic": "Улыбка", "care_type": "Стоматология"}
