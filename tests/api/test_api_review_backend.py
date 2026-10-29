"""Ревью бэкенда 29.10.2026: «не загружен» — только когда договора правда нет (п. 3), вопрос не остаётся без ответа
при сбое расчёта (п. 4), два вопроса в одном обращении не считаются одновременно (п. 5)."""
from __future__ import annotations

import logging

import pytest

from api.api_kit import FakeEngine, make_client, new_request
from dms_api.service import ServiceError
from dms_core.store.db import ContractNotLoaded


class RaisingEngine(FakeEngine):
    def __init__(self, exc: BaseException):
        super().__init__()
        self.exc = exc

    def __call__(self, question, context, *, llm=None, store=None):
        raise self.exc


class _Records(logging.Handler):
    def __init__(self) -> None:
        super().__init__(logging.ERROR)
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(record.getMessage())


def test_internal_keyerror_is_500_and_logged_not_contract_not_loaded():
    c, svc, _ = make_client(engine=RaisingEngine(KeyError("slot")))
    _chat, rid = new_request(c)
    rec = _Records()
    log = logging.getLogger("dms.service")   # обработчик на сам журнал: настройка журналов другими тестами не мешает
    level, disabled = log.level, log.disabled
    log.addHandler(rec)
    log.setLevel(logging.INFO)
    log.disabled = False
    try:
        r = c.post(f"/api/requests/{rid}/messages", json={"text": "Входит ли стоматология?"})
    finally:
        log.removeHandler(rec)
        log.setLevel(level)
        log.disabled = disabled
    assert r.status_code == 500 and r.json()["error"]["code"] == "internal"
    assert any("KeyError в" in m for m in rec.messages)
    # п. 4: вопрос не «висит» — за ним ответ ассистента с текстом ошибки
    msgs = svc.app.request(rid).messages
    assert [m.role for m in msgs] == ["user", "assistant"] and msgs[-1].text and msgs[-1].answer is None


def test_contract_removed_during_answer_is_422_with_error_reply():
    c, svc, _ = make_client(engine=RaisingEngine(ContractNotLoaded("договор c01 не загружен")))
    _chat, rid = new_request(c)
    r = c.post(f"/api/requests/{rid}/messages", json={"text": "Входит ли стоматология?"})
    assert r.status_code == 422 and r.json()["error"]["code"] == "contract_not_loaded"
    msgs = svc.app.request(rid).messages
    assert msgs[-1].role == "assistant" and "не загружен" in msgs[-1].text


def test_second_question_while_answering_is_409_then_free():
    c, svc, _ = make_client(engine=FakeEngine())
    _chat, rid = new_request(c)
    svc.prepare_message(rid, {"text": "Первый вопрос"})              # расчёт первого вопроса идёт
    with pytest.raises(ServiceError) as e:
        svc.prepare_message(rid, {"text": "Второй вопрос"})
    assert e.value.status == 409 and e.value.code == "request_busy"
    r = c.post(f"/api/requests/{rid}/messages", json={"text": "Второй вопрос"})
    assert r.status_code == 409
    svc.release_busy(rid)
    r = c.post(f"/api/requests/{rid}/messages", json={"text": "Второй вопрос"})
    assert r.status_code == 200
    # после ответа обращение снова свободно
    assert c.post(f"/api/requests/{rid}/messages", json={"text": "Третий вопрос"}).status_code == 200


def test_clarify_answer_is_glued_to_original_question():
    """«Что уточнить» → кнопка «Да» или свой ответ: сообщение с clarify; движку — исходный вопрос + «вопрос — ответ»,
    в ленте — «вопрос — ответ» (а не голое «Да»)."""
    eng = FakeEngine()
    c, svc, _ = make_client(engine=eng)
    _chat, rid = new_request(c)
    assert c.post(f"/api/requests/{rid}/messages", json={"text": "Отказали в госпитализации — правомерно?"}).status_code == 200
    q = "Есть ли направление врача и согласование страховщика (гарантийное письмо)?"
    r = c.post(f"/api/requests/{rid}/messages", json={"text": "Да", "clarify": q})
    assert r.status_code == 200
    assert eng.calls[-1][0] == ("Отказали в госпитализации — правомерно? направление врача и согласование "
                                "страховщика (гарантийное письмо) есть.")
    user = [m for m in svc.app.request(rid).messages if m.role == "user"][-1]
    assert user.text == f"{q} — Да" and user.asked == eng.calls[-1][0]
    # слишком длинное уточнение — 422 до расчёта, обращение не остаётся занятым
    assert c.post(f"/api/requests/{rid}/messages", json={"text": "Да", "clarify": "x" * 400}).status_code == 422
    assert c.post(f"/api/requests/{rid}/messages", json={"text": "Нет", "clarify": q}).status_code == 200


def test_clarify_fact_words():
    from dms_core.followup import clarify_fact
    assert clarify_fact("Есть ли направление врача?", "Нет") == "направление врача нет."
    assert clarify_fact("Экстренная или плановая госпитализация?", "Плановая госпитализация") == "Плановая госпитализация."
    assert clarify_fact("Экстренная или плановая госпитализация?", "Экстренная") == "Экстренная."
    assert clarify_fact("В какой клинике отказали?", "Медси") == "В клинике Медси."
    assert clarify_fact("Какую причину отказа назвала клиника?", "нет мест") == "Причина: нет мест."
    assert clarify_fact("Сколько дней длилась госпитализация?", "5") == "Сколько дней длилась госпитализация — 5."


def test_letter_from_answer_and_check():
    """Письмо HR / в СК из ответа: шаблон drafts.yaml letters, без обещаний; проверка после правки."""
    c, _svc, _ = make_client(engine=FakeEngine())
    _chat, rid = new_request(c)
    assert c.post(f"/api/requests/{rid}/letter", json={"to": "hr"}).status_code == 409        # ответа ещё нет
    assert c.post(f"/api/requests/{rid}/messages", json={"text": "Входит ли стоматология?"}).status_code == 200
    r = c.post(f"/api/requests/{rid}/letter", json={"to": "hr"})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["to"] == "hr" and d["template"] == "answer_hr" and "Добрый день" in d["body"] and d["subject"]
    assert "страховая компания" in d["body"]                     # оговорка о решении СК
    ins = c.post(f"/api/requests/{rid}/letter", json={"to": "insurer"}).json()
    assert ins["to"] == "insurer" and "Просим" in ins["body"]
    assert c.post(f"/api/requests/{rid}/letter", json={"to": "boss"}).status_code == 422
    ok = c.post("/api/letters/check", json={"subject": "Тема", "body": "Текст без обещаний."}).json()
    assert ok == {"promises": [], "unfilled": 0}
    bad = c.post("/api/letters/check", json={"subject": "Тема", "body": "Гарантируем оплату. [ЗАПОЛНИТЬ]"}).json()
    assert bad["promises"] and bad["unfilled"] == 1
