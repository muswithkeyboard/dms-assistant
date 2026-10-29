"""Ответ на уточняющий вопрос продолжает исходный вопрос (followup.py, w16)."""
import datetime as dt

from dms_core.constants import Confidence, Scenario
from dms_core.followup import followup_question, is_short_reply
from dms_core.schemas import Answer, AnswerContext, Message

NOW = dt.datetime(2026, 10, 27, tzinfo=dt.UTC)


def _ans(conf: Confidence) -> Answer:
    return Answer(scenario=Scenario.sc01, context=AnswerContext(contract_id="c01"), summary="s", confidence=conf,
                  questions=["Какая программа у застрахованного?"])


def _msgs(conf: Confidence, asked: str | None = None) -> list[Message]:
    return [Message(role="user", text="Клиника не дала сделать МРТ — правомерно?", asked=asked, created_at=NOW),
            Message(role="assistant", answer=_ans(conf), created_at=NOW)]


def test_short_reply_to_clarification_is_glued_to_question():
    got = followup_question(_msgs(Confidence.need_clarification), "Стандарт")
    assert got == "Клиника не дала сделать МРТ — правомерно? По программе «Стандарт»."


def test_chain_uses_already_glued_question():
    msgs = _msgs(Confidence.need_clarification, asked="Клиника не дала сделать МРТ — правомерно? В клинике Медси.")
    assert followup_question(msgs, "Стандарт").endswith("В клинике Медси. По программе «Стандарт».")


def test_new_question_or_normal_answer_is_not_glued():
    assert followup_question(_msgs(Confidence.need_clarification), "Входит ли стоматология?") is None
    assert followup_question(_msgs(Confidence.confirmed), "Стандарт") is None
    assert followup_question([], "Стандарт") is None
    assert not is_short_reply("раз два три четыре пять шесть семь восемь девять")


def test_unknown_program_answer_is_replaced_not_accumulated():
    from dms_core.followup import glued_reply

    assert glued_reply("Можно ли вырвать зуб? По программе «омс».") == ("программ", "омс")
    assert glued_reply("Можно ли вырвать зуб?") is None
    # ассистент снова спросил программу — новый ответ заменяет неподошедший «омс»
    msgs = _msgs(Confidence.need_clarification, asked="Можно ли вырвать зуб? По программе «омс».")
    assert followup_question(msgs, "Муром Стандарт") == "Можно ли вырвать зуб? По программе «Муром Стандарт»."
