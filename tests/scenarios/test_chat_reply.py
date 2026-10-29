"""Ответ в чате (ADR 0014, CONTRACTS §12): compose.chat_reply собирает lead / points / ask / caution КОДОМ из
проверенных утверждений. Тесты на своих синтетических ответах (без документов организаторов) и один — на комплекте
(SKIP без базы разбора)."""
from __future__ import annotations

import re

from dms_core.citations import normalize
from dms_core.compose import (
    SERVICE_ID,
    _guided_texts,
    _reply_texts,
    chat_reply,
    cite_ref,
    reply_quote,
    sentences,
)
from dms_core.constants import (
    REPLY_POINTS_MAX,
    REPLY_QUOTE_CHARS,
    AnswerMode,
    Confidence,
    Label,
    Scenario,
    StatementKind,
)
from dms_core.schemas import (
    Answer,
    AnswerContext,
    Citation,
    ClinicChoice,
    ClinicRow,
    PerContract,
    Statement,
)
from helpers import kit_db_or_skip

ORDER_QUOTE = ("организуется Страховщиком по направлению лечащего врача при наличии следующей документации: "
               "направление на плановую госпитализацию, необходимые результаты догоспитального обследования.")
EMERG_QUOTE = ("В исключительных случаях, по жизненным показаниям, экстренная госпитализация может быть осуществлена "
               "в ближайшую к месту нахождения Застрахованного государственную (муниципальную) больницу.")


def cit(quote: str, *, cid: str = "c01", ref: str = "t6r0p332", verified: bool = True,
        clause: str | None = None) -> Citation:
    return Citation(doc_id=f"{cid}.program", doc_title="Приложение 1. Программа ДМС", contract_id=cid,
                    clause_number=clause, table_ref=None if clause else f"{cid}.program#{ref}", quote=quote,
                    verified=verified)


def st(text: str, quote: str, kind: StatementKind = StatementKind.fact, **kw) -> Statement:
    return Statement(text=text, kind=kind, citations=[cit(quote, **kw)])


def ans(statements: list[Statement], *, label: Label | None = None, scenario: Scenario = Scenario.sc01,
        **kw) -> Answer:
    return Answer(scenario=scenario, context=AnswerContext(contract_id="c01", program="Москва"),
                  summary=kw.pop("summary", ""), label=label, confidence=Confidence.confirmed,
                  statements=statements, **kw)


def g(key: str, **kw) -> str:
    return _guided_texts()[key].format(**kw)


def care_in() -> Statement:
    return st(g("care_matrix_yes", care="МРТ", program="Москва"), "Москва: МРТ — входит", ref="t5r10")


def all_texts(r) -> list[str]:
    return [r.lead, r.caution or "", *r.ask, *(p.text for p in r.points)]


def test_lead_templates_for_every_label():
    """lead SC 01 — «<Метка словами>: <одна фраза>» для всех меток (образ ответа заказчика, слайд 13)."""
    words = _reply_texts()["label_words"]
    for label in Label:
        r = chat_reply(ans([care_in()], label=label))
        assert r.lead.startswith(f"{words[label.value]}: "), r.lead
        assert len(sentences(r.lead)) <= 2 and len(r.lead) <= 300
        assert "МРТ" in r.lead
        assert "матриц" not in r.lead and "таблица видов помощи программы" in r.lead


def test_sc02_info_lead_is_main_fact_without_label():
    pay = st(g("payment_next", n=4, amount="56 760,00", due="31.10.2026", period=""), "4 | 31.10.2026 | 56.760,00",
             ref="t3r4")
    r = chat_reply(ans([pay], scenario=Scenario.sc02_info))
    assert r.lead == "Ближайший взнос № 4 — 56 760,00 руб., оплатить до 31.10.2026."
    assert r.points[0].text == r.lead


def test_this_clause_replaced_by_quote_content():
    """«… по порядку этого пункта: сверьте с этим пунктом» → тема + содержание пункта из цитаты."""
    planned = st(g("refusal_order_planned", by_prog="программе «Москва»"), ORDER_QUOTE, StatementKind.condition)
    emerg = st(g("refusal_order_emergency", by_prog="программе «Москва»"), EMERG_QUOTE, StatementKind.condition,
               ref="t6r0p329")
    r = chat_reply(ans([planned, emerg], label=Label.insufficient))
    texts = [p.text for p in r.points]
    assert all("пункт" not in t for t in texts), texts
    assert texts[0].startswith("Плановая госпитализация по программе «Москва» — по направлению врача")
    assert texts[1].startswith("Экстренная госпитализация по программе «Москва» — по жизненным показаниям")
    for p, s in zip(r.points, (planned, emerg), strict=True):
        assert p.quote and normalize(p.quote) in normalize(s.citations[0].quote)
        assert len(p.quote) <= REPLY_QUOTE_CHARS


def test_this_clause_without_markers_uses_quote_start():
    s = st("Порядок записи к врачу — по этому пункту: сверьте с ним.", "Запись производится через пульт Страховщика.",
           StatementKind.condition)
    r = chat_reply(ans([s], scenario=Scenario.sc02_info))
    assert r.points[0].text == "Порядок записи к врачу: запись производится через пульт Страховщика."


def test_no_service_ids_or_words():
    s = st("Строка c06.program#t5r250 (матрица видов помощи) supports: c06 — входит.", "Москва: МРТ — входит")
    r = chat_reply(ans([s], label=Label.allowed, missing=["c01: Какую причину отказа назвала клиника?"]))
    for t in all_texts(r):
        assert not SERVICE_ID.search(t), t
        assert not re.search(r"supports|guided|матриц", t), t
    assert r.ask == ["Договор № 1: Какую причину отказа назвала клиника?"]


def test_points_only_with_verified_refs():
    good = care_in()
    bad = st("Непроверенное утверждение.", "текст", verified=False, ref="t9r9")
    r = chat_reply(ans([bad, good], label=Label.allowed))
    assert len(r.points) == 1
    live = {cite_ref(c) for c in good.citations}
    assert all(p.refs and set(p.refs) <= live for p in r.points)


def test_points_order_dedup_and_limit():
    """Основание метки (для «нельзя» — исключение) первым; дубли по тексту — один раз; ≤ REPLY_POINTS_MAX."""
    excl = st(g("exclusion_yes", program="Москва"), "лечение пародонта", StatementKind.limitation, ref="t7r1")
    many = [st(f"Справка {i}.", f"цитата {i}", ref=f"t8r{i}") for i in range(10)]
    r = chat_reply(ans([*many, care_in(), care_in(), excl], label=Label.denied))
    assert r.points[0].text.startswith("Услуга названа в исключениях")
    assert r.lead.startswith("Нельзя: услуга названа в исключениях")
    assert len(r.points) == REPLY_POINTS_MAX
    assert len({p.text for p in r.points}) == len(r.points)


def test_conditional_lead_names_conditions_from_quotes():
    cond = st("Условие обслуживания — по этому пункту.", ORDER_QUOTE, StatementKind.condition)
    r = chat_reply(ans([care_in(), cond], label=Label.conditional))
    assert r.lead.startswith("Можно при условиях: «МРТ» — входит в программу «Москва»")
    assert "условия: по направлению врача" in r.lead


def test_refusal_lead_reason_and_ask():
    """Разбор отказа: основания из краткого вывода кода; причина отказа из вопроса — в lead и не переспрашивается."""
    summary = " ".join([
        g("refusal_sum_care", care="Госпитализация", program="Москва", service="ампутация", klass="операция"),
        g("refusal_sum_grounds", grounds=g("refusal_ground_planned", num=" (п. 5)")),
        g("refusal_sum_emergency", of_prog="программы «Москва»", num=""),
    ])
    a = ans([care_in()], label=Label.insufficient, summary=summary,
            missing=[g("refusal_missing_reason"), g("refusal_missing_mode")])
    r = chat_reply(a, question="хирург отказался ампутировать стопу, говорит не по полису — так можно?")
    assert r.lead.startswith("Недостаточно данных: отказ может быть обоснован, если госпитализация плановая")
    assert "если случай экстренный" in r.lead
    assert r.lead.endswith("Причина отказа со слов клиники: «не по полису».")
    assert len(sentences(r.lead)) == 2 and len(r.lead) <= 300
    assert g("refusal_missing_reason") not in r.ask and r.ask
    r2 = chat_reply(a, question="отказали в операции")
    assert "Причина отказа" not in r2.lead and g("refusal_missing_reason") in r2.ask


def test_clarify_lead_is_question():
    a = Answer(scenario=Scenario.sc02_info, context=AnswerContext(group_id="gk1"), summary="Нужно уточнение.",
               confidence=Confidence.need_clarification, questions=["Какая программа у застрахованного?",
                                                                    "Какая программа у застрахованного?"])
    r = chat_reply(a)
    assert r.lead == "Какая программа у застрахованного?" and r.points == [] and r.ask == []


def test_clinic_choice_pending_second_sentence():
    row = ClinicRow(id="c01.clinics#1", contract_id="c01", program="Москва", clinic_name="Клиника", care_type="Поликлиника",
                    doc_id="c01.clinics",
                    source_ref="Лист1!1")
    a = ans([care_in()], label=Label.insufficient,
            clinic_choices=[ClinicChoice(contract_id="c01", program="Москва", asked="клиника", candidates=[row])])
    r = chat_reply(a)
    assert r.lead.endswith(_reply_texts()["lead_clinic_choice"])


def test_extractive_mode():
    a = ans([st("3. Плановая госпитализация организуется по направлению.", "Плановая госпитализация организуется по "
                "направлению.", clause="3")], scenario=Scenario.sc02_info, mode=AnswerMode.extractive)
    r = chat_reply(a)
    assert r.lead == _reply_texts()["lead_extractive"]
    assert r.points[0].text == "Плановая госпитализация организуется по направлению."
    assert r.caution == _reply_texts()["caution_extractive"]


def test_group_lead_and_captions():
    """ГК: сводная метка; пункты — с подписью «Договор № N» и contract_id; одинаковый пункт — один раз с ссылками
    каждого договора (условие не переносится: у каждого договора своя ссылка)."""
    def pc(cid: str, label: Label, text: str = "Вид помощи «МРТ» входит в программу «Москва».") -> PerContract:
        return PerContract(contract_id=cid, summary="", label=label,
                           statements=[st(text, "Москва: МРТ — входит", cid=cid)])
    same = Answer(scenario=Scenario.sc01, context=AnswerContext(group_id="gk2"), summary="",
                  confidence=Confidence.confirmed, per_contract=[pc("c05", Label.allowed), pc("c06", Label.allowed)])
    r = chat_reply(same)
    assert r.lead.startswith("Можно — по всем договорам группы (2): вид помощи «МРТ» входит")
    assert len(r.points) == 1 and r.points[0].text.startswith("Договоры № 5, 6: ")
    assert set(r.points[0].refs) == {"c05.program#t6r0p332", "c06.program#t6r0p332"}
    mixed = same.model_copy(update={"per_contract": [pc("c05", Label.allowed, "А входит."),
                                                     pc("c06", Label.insufficient, "Б не найдено."),
                                                     pc("c07", Label.allowed, "В входит.")]})
    r = chat_reply(mixed)
    assert r.lead == "По договорам группы: по 2 из 3 — можно; по 1 из 3 — недостаточно данных."
    assert [p.contract_id for p in r.points] == ["c05", "c06", "c07"]
    assert r.points[1].text == "Договор № 6: Б не найдено."
    for t in all_texts(r):
        assert not SERVICE_ID.search(t), t
    # одна метка у всех, но пункт есть только у второго договора: фраза — из него и с его номером, а не «не найдено»
    empty = PerContract(contract_id="c05", summary="", label=Label.insufficient, statements=[])
    some = same.model_copy(update={"per_contract": [empty, pc("c06", Label.insufficient)]})
    r = chat_reply(some)
    assert r.lead.startswith("Недостаточно данных — по всем договорам группы (2). Договор № 6: "), r.lead
    assert "не найдено подтверждённых условий" not in r.lead and len(r.lead) <= 300


def test_reply_quote_cut_by_phrase():
    long = "Первая фраза пункта о порядке организации помощи застрахованным по программе страхования. " + "Слово " * 60
    q = reply_quote("- " + long)
    assert q == "Первая фраза пункта о порядке организации помощи застрахованным по программе страхования."
    q2 = reply_quote("слово " * 80)
    assert len(q2) <= REPLY_QUOTE_CHARS and normalize(q2) in normalize("слово " * 80)


def test_sentences_keep_quoted_names_and_numbers():
    assert sentences("«Программа. Стандарт» — входит. Второе.") == ["«Программа. Стандарт» — входит.", "Второе."]
    assert sentences("3. Терапевтические методы.") == ["3. Терапевтические методы."]


def test_compose_fills_reply():
    from dms_core.citations import Verified
    from dms_core.compose import compose
    from dms_core.schemas import DraftAnswer, DraftCitation, DraftStatement

    c = cit("Москва: МРТ — входит")
    draft = DraftAnswer(contract_id="c01", summary="", statements=[DraftStatement(
        text="Вид помощи «МРТ» входит в программу «Москва».", citations=[DraftCitation(ref="x", quote=c.quote)])])
    v = Verified(draft=draft, citations={"x": c}, dropped=[])
    out = compose(Scenario.sc01, AnswerContext(contract_id="c01"), {"c01": v}, label=Label.allowed)
    assert out.reply is not None and out.reply.lead.startswith("Можно: вид помощи «МРТ» входит")


def test_kit_refusal_reply_has_content():
    """На комплекте: разбор отказа даёт ответ словами — без «этого пункта», с содержанием пунктов порядка."""
    store = kit_db_or_skip()
    from dms_core.engine import answer
    from dms_core.schemas import ChatContext

    c = store.contracts("gk2")[0].id if store.contracts("gk2") else None
    if c is None:
        return
    a = answer("хирург отказался ампутировать стопу — так можно?",
               ChatContext(contract_id=c, program=store.contract(c).programs[0].name if store.contract(c).programs
                           else None), store=store)
    r = chat_reply(a, question="хирург отказался ампутировать стопу — так можно?", store=store)
    assert r.lead and len(r.lead) <= 300 and len(sentences(r.lead)) <= 2
    assert all("этого пункта" not in p.text and "этим пунктом" not in p.text for p in r.points)
    for t in all_texts(r):
        assert not SERVICE_ID.search(t), t
    live = {cite_ref(c) for s in a.statements for c in s.citations if c.verified}
    assert all(p.refs and set(p.refs) <= live for p in r.points)


def test_refs_are_citation_ref_of_verified():
    """refs пункта — Citation.ref проверенной ссылки (verify заполняет его); без ref — строка таблицы."""
    c = cit("Москва: МРТ — входит").model_copy(update={"ref": "c01.program#t5r10"})
    r = chat_reply(ans([Statement(text="Вид помощи «МРТ» входит.", citations=[c])], label=Label.allowed))
    assert r.points[0].refs == ["c01.program#t5r10"]
    assert cite_ref(cit("x", clause="4.1")) == "c01.program:4.1"


def test_oms_question_gets_dms_only_caution():
    r = chat_reply(ans([care_in()], label=Label.insufficient), question="Можно ли вырвать зуб по ОМС?")
    assert "Об ОМС ассистент не отвечает" in r.caution
    r = chat_reply(ans([care_in()], label=Label.insufficient), question="Можно ли вырвать зуб?")
    assert "ОМС" not in r.caution


def test_points_only_about_service_of_question():
    """«Вырвать зуб» — пункты об удалении зубов, без чистки («удаление зубных отложений») и пломб (ревизия w18)."""
    pull = st("Хирургическая стоматология: удаление зубов (простое, сложное).",
              "Хирургическая стоматология: удаление зубов (простое, сложное).", ref="p1")
    clean = st("Профилактика: удаление зубных отложений ультразвуком.",
               "Профилактика: удаление зубных отложений ультразвуком.", ref="p2")
    fill = st("Постановка пломбы при разрушении коронковой части зуба.",
              "Постановка пломбы при разрушении коронковой части зуба.", ref="p3")
    r = chat_reply(ans([pull, clean, fill], label=Label.conditional), question="можно ли вырвать зуб")
    assert [p.text for p in r.points] == ["Хирургическая стоматология: удаление зубов (простое, сложное)."]
    # ни одного пункта о самой услуге — показываются все (lead скажет, что прямого ответа нет)
    r = chat_reply(ans([clean, fill], label=Label.insufficient), question="можно ли вырвать зуб")
    assert len(r.points) == 2


def test_vague_question_gets_narrowing_options():
    r = chat_reply(ans([], label=None, scenario=Scenario.general), question="а зубы?")
    assert "Входит ли удаление зуба?" in r.options and r.lead.startswith("Ответ зависит")
    r = chat_reply(ans([care_in()], label=Label.allowed), question="Входит ли удаление зуба?")
    assert r.options == []


def test_lead_of_long_list_shows_part_about_service():
    """Длинный пункт-перечень в lead — начало пункта и часть с услугой, а не обрезанное начало (ревизия w18)."""
    from dms_core.compose import _around

    q = ("Зубопротезирование и подготовка к нему (за исключением случаев, предусмотренных Программой), включая "
         "перелечивание корневых каналов, удаление кист, удаление и депульпирование зубов и пр. подготовительные "
         "работы; имплантация зубов.")
    got = _around(q, ["удаление", "зуб"])
    assert got.startswith("Зубопротезирование и подготовка к нему …") and "депульпирование зубов" in got
    assert "(за исключением" not in got and len(got) <= 160
    short = "Услуги, оказываемые в косметических целях: отбеливание зубов, реставрация зубных рядов."
    assert _around(short * 3, ["косметический"]).startswith("Услуги, оказываемые в косметических целях")


def test_key_lemmas_of_question():
    """Ключевые слова услуги — существительные без общих слов процедуры, условий и названий (ревизия w18)."""
    from dms_core.compose import _key_lemmas

    assert _key_lemmas("Сотрудник хочет удалить родинку по косметическим показаниям, это покрыто?") == ["родинка"]
    assert _key_lemmas("Можно ли к эндокринологу без направления терапевта?") == ["эндокринолог"]
    assert _key_lemmas("Оплатят ли лечение ВИЧ-инфекции?") == ["вич", "инфекция"]
    assert _key_lemmas("Покрывается ли МРТ коленного сустава?") == ["мрт"]          # анатомия — уточнение
    assert _key_lemmas("Покрывается ли медикаментозное обеспечение?") == ["медикаментозный"]
    assert _key_lemmas("Можно ли полечиться в клинике Мать и дитя?") == []          # название клиники — не услуга


def test_main_point_must_name_the_service():
    """Главный пункт — тот, где названа сама услуга; нет такого — lead так и говорит, без «ближайшего» о другом."""
    cosm = st("Услуги в косметических целях: отбеливание зубов, реставрация зубных рядов.",
              "Услуги в косметических целях: отбеливание зубов, реставрация зубных рядов.", ref="p9")
    r = chat_reply(ans([cosm], label=Label.insufficient),
                   question="Сотрудник хочет удалить родинку по косметическим показаниям, это покрыто?")
    assert "прямо не названо: «родинка»" in r.lead and "отбеливание" not in r.lead
    mole = st("Удаление доброкачественных новообразований кожи (родинок) по медицинским показаниям.",
              "Удаление доброкачественных новообразований кожи (родинок) по медицинским показаниям.", ref="p8")
    r = chat_reply(ans([cosm, mole], label=Label.insufficient),
                   question="Сотрудник хочет удалить родинку по косметическим показаниям, это покрыто?")
    assert "родинок" in r.lead


def test_key_lemmas_program_names_and_question_words():
    """Название программы — целым словом («МО» не режет «Можно»); «есть» — слово вопроса, не услуга (ревизия w18)."""
    from dms_core.compose import _CONTEXT_NAMES, _key_lemmas

    token = _CONTEXT_NAMES.set(("МО", "ВИП"))
    try:
        assert _key_lemmas("Можно ли к гинекологу в ВИП?") == ["гинеколог"]
    finally:
        _CONTEXT_NAMES.reset(token)
    assert _key_lemmas("Есть ли франшиза?") == ["франшиза"]


def test_synonyms_are_phrases_not_loose_words():
    """«Зуб» из синонима «имплантация зуба» — не синоним «имплантации»; «МРТ» = «магнитно-резонансная томография»."""
    from dms_core.compose import _phrase_hit, _synonyms
    from dms_core.index.morph import lemmas

    assert ("имплантация",) not in _synonyms("зуб")
    have = set(lemmas("магнитно-резонансная томография головного мозга"))
    assert any(_phrase_hit(p, have) for p in _synonyms("мрт"))


def test_main_point_prefers_direct_row_and_full_clause():
    """Равные пункты: строка таблицы «— да» и полный пункт главнее обрывка перечня (ревизия w18)."""
    full = st("Диагностика и лечение сахарного диабета I и II типа (кроме осложнений сахарного диабета).",
              "Диагностика и лечение сахарного диабета I и II типа (кроме осложнений сахарного диабета).", ref="p1")
    frag = st("25. для лечения сахарного диабета;", "25. для лечения сахарного диабета;", ref="p2")
    r = chat_reply(ans([frag, full], label=Label.conditional), question="Оплачивается ли лечение сахарного диабета?")
    assert "I и II типа" in r.lead
