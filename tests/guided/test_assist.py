"""Модель-помощник (assist.py, w17) без сети: переписанный вопрос берётся, только если не добавляет своего и маршрут
стал лучше; обычные реплики — ответ из конфига."""
from __future__ import annotations

import json

from dms_core.assist import rephrase, reset_pause
from dms_core.constants import Scenario
from dms_core.engine import answer
from dms_core.inputcheck import smalltalk
from dms_core.llm.base import LlmError, LlmTurn, Usage
from dms_core.schemas import ChatContext
from dms_core.store import KitStore
from fixtures.synth_kit import make_parsed
from helpers import env


class StubLLM:
    """Отвечает заранее заданным вопросом; запоминает, сколько раз её спросили."""
    provider, model = "stub", "stub"

    def __init__(self, question: str) -> None:
        self.question, self.calls = question, 0

    def chat(self, messages, tools=None, *, json_schema=None, max_tokens=4096):
        self.calls += 1
        return LlmTurn(text=json.dumps({"question": self.question}, ensure_ascii=False), tool_calls=[],
                       usage=Usage(), model="stub")


def test_rephrase_keeps_only_words_of_question_and_dictionary():
    q = "а сотрудника к стоматологу пустят?"
    assert rephrase(q, StubLLM("Входит ли стоматология в программу?")) == "Входит ли стоматология в программу?"
    # клиника, которой не было в вопросе, — своё модели: отказ
    assert rephrase(q, StubLLM("Входит ли стоматология в клинике «Медси»?")) is None
    assert rephrase(q, StubLLM("Входит ли стоматология 5 раз в год?")) is None
    assert rephrase(q, StubLLM("")) is None
    # подмена услуги словом словаря, не связанным со словами вопроса («лечить зубы» ≠ «удаление»): ложное «нельзя»
    assert rephrase("а зубы лечить будут?", StubLLM("Входит ли удаление зуба в программу?")) is None
    assert rephrase("можно ли вырвать зуб", StubLLM("Входит ли удаление зуба в программу?")) is not None
    assert rephrase("к лору можно?", StubLLM("Входит ли приём оториноларинголога в программу?")) is not None


def _store() -> KitStore:
    st = KitStore(":memory:")
    st.save(make_parsed("c01"))
    return st


def test_engine_uses_rephrase_only_when_rules_did_not_understand():
    st, ctx = _store(), ChatContext(group_id="gk9", contract_id="c01")
    with env(DMS_ENGINE_MODE="guided"):
        # «а к зубному сходить…» правила относят к общему сценарию — помощник переписывает вопрос
        llm = StubLLM("Входит ли стоматология в программу?")
        a = answer("а к зубному сходить можно будет без денег?", ctx, llm=llm, store=st)
        assert llm.calls == 1 and a.reply.understood == "Входит ли стоматология в программу?"
        assert a.scenario is not Scenario.general
        # вопрос, понятый правилами, модель не трогает
        llm2 = StubLLM("Входит ли МРТ в программу?")
        a2 = answer("Входит ли стоматология в программу?", ctx, llm=llm2, store=st)
        assert llm2.calls == 0 and a2.reply.understood is None


def test_rephrase_with_new_clinic_is_not_used():
    st, ctx = _store(), ChatContext(group_id="gk9", contract_id="c01")
    with env(DMS_ENGINE_MODE="guided"):
        a = answer("а к зубному сходить можно?", ctx, llm=StubLLM("Есть ли клиника «Медси» в перечне?"), store=st)
    assert a.reply.understood is None


def test_smalltalk_whole_message_only():
    assert smalltalk("Привет!").startswith("Здравствуйте")
    assert smalltalk("спасибо большое").startswith("Пожалуйста")
    assert "по документам договора" in smalltalk("что ты умеешь?")
    assert smalltalk("спасибо, а МРТ входит?") is None
    assert smalltalk("Помощь на дому входит?") is None
    st = _store()
    a = answer("привет", ChatContext(group_id="gk9", contract_id="c01"), store=st)
    assert a.reply.lead.startswith("Здравствуйте") and not a.statements


def test_rephrase_called_once_per_question():
    # правила не поняли вопрос («к лору сходить» — общий сценарий): модель вызвана один раз, вопрос понят как
    # стандартный; повторного вызова «ничего не нашлось» после принятой трактовки нет
    st, ctx = _store(), ChatContext(group_id="gk9", contract_id="c01")
    with env(DMS_ENGINE_MODE="guided"):
        llm = StubLLM("Входит ли приём оториноларинголога в программу?")
        a = answer("сотрудник хочет к лору сходить", ctx, llm=llm, store=st)
    assert llm.calls == 1 and a.reply.understood == "Входит ли приём оториноларинголога в программу?"


def test_nothing_found_retries_once_with_rephrase():
    # правила взяли разговорное слово за услугу («зубнику») и ничего не нашли — повтор с вопросом модели
    st, ctx = _store(), ChatContext(group_id="gk9", contract_id="c01")
    with env(DMS_ENGINE_MODE="guided"):
        llm = StubLLM("Входит ли стоматология в программу?")
        a = answer("сотрудник хочет к зубнику, это бесплатно?", ctx, llm=llm, store=st)
    assert llm.calls == 1 and a.reply.understood == "Входит ли стоматология в программу?" and a.statements


class DownLLM:
    """Модель недоступна: каждый вызов — LlmError (как Ollama, которая не отвечает)."""
    provider, model = "down", "down"

    def __init__(self) -> None:
        self.calls = 0

    def chat(self, messages, tools=None, *, json_schema=None, max_tokens=4096):
        self.calls += 1
        raise LlmError("нет связи с провайдером")


def test_rephrase_pauses_after_model_failure():
    """После сбоя модели помощник не вызывается до конца паузы — вопрос своими словами не ждёт таймаут каждый раз."""
    reset_pause()
    down = DownLLM()
    try:
        assert rephrase("а зубы?", down) is None
        assert rephrase("а массаж?", down) is None
        assert down.calls == 1
        ok = StubLLM("Входит ли стоматология в программу?")
        assert rephrase("а стоматология входит?", ok) is None and ok.calls == 0   # пауза общая для процесса
        reset_pause()
        assert rephrase("а стоматология входит?", ok) == "Входит ли стоматология в программу?"
    finally:
        reset_pause()
