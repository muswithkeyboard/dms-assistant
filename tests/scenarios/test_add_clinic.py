"""Автомат добавления клиники (ТЗ п. 5.2.2, ADR 0004): начальные состояния, все ветки, недопустимые переходы,
идемпотентность. Синтетический комплект, без документов организаторов и без модели."""
from __future__ import annotations

import datetime as dt
import re

from dms_core.constants import AddClinicState as S
from dms_core.constants import Confidence, Scenario
from dms_core.promises import find_promises
from dms_core.scenarios import add_clinic
from dms_core.scenarios.drafts import FILL
from dms_core.schemas import Answer, AnswerContext, ClinicRow, Request
from dms_core.store import KitStore
from fixtures.synth_kit import make_parsed

DENTAL = "Стоматологическая помощь"


def kit() -> KitStore:
    """c01: «Стандарт» — «Улыбка» и «Дента-Плюс» (стоматология); «Премиум» — «Жемчуг». c02 — другой договор ГК
    с «Медси» в той же по названию программе «Стандарт» (ловушка ADR 0005)."""
    st = KitStore(":memory:")
    p1 = make_parsed("c01")
    p1.clinics.append(ClinicRow(id="c01.clinics#t0r9", contract_id="c01", doc_id="c01.clinics", program="Стандарт",
                                care_type=DENTAL, clinic_name="Клиника «Дента-Плюс»", address="г. Москва, ул. Ленина, 2",
                                city="Москва", franchise_pct="—", source_ref="t0:r9"))
    p1.clinics.append(ClinicRow(id="c01.clinics#t1r0", contract_id="c01", doc_id="c01.clinics", program="Премиум",
                                care_type=DENTAL, clinic_name="Клиника «Жемчуг»", address="г. Москва, ул. Арбат, 5",
                                city="Москва", source_ref="t1:r0"))
    st.save(p1)
    st.save(make_parsed("c02"))
    return st


def answer(contract_id: str | None = "c01", program: str | None = "Стандарт") -> Answer:
    return Answer(scenario=Scenario.sc02_add_clinic, context=AnswerContext(group_id="gk9", contract_id=contract_id,
                  program=program), summary="", confidence=Confidence.need_clarification)


def req(**slots: str) -> Request:
    return Request(id="r1", chat_id="ch1", title="Добавить клинику", slots={"care_type": "стоматология", **slots})


def started(clinic: str = "Жемчуг", st: KitStore | None = None) -> tuple[Request, KitStore]:
    st = st or kit()
    r = req()
    add_clinic.start(r, answer(), clinic=clinic, store=st)
    return r, st


def run(r: Request, *actions: str, st: KitStore, reply: str | None = None) -> Request:
    for a in actions:
        add_clinic.step(r, a, insurer_reply=reply if a in add_clinic.INSURER_ACTIONS else None, store=st)
    return r


def no_promises(r: Request) -> None:
    for d in r.drafts:
        assert d.auto_send is False
        # «клиника добавлена» допустима только в письме HR после подтверждения СК (allow_in шаблона hr_clinic_added)
        tid = "hr_clinic_added" if d.to.value == "hr" and d.stage in add_clinic.ADDED_STATES else None
        assert find_promises(d.subject + "\n" + d.body, tid) == [], d.subject


# ------------------------------------------------------------------------------------------- таблица переходов

# MUST ANSWER 1: таблица переходов ADR 0004 / config/add_clinic.yaml — проверяется целиком этим тестом.
TABLE = {
    S.not_in_program:   {"hr_wants_add": S.hr_wants_add, "close": S.closed},
    S.hr_wants_add:     {"insurer_request_ready": S.awaiting_insurer, "close": S.closed},
    S.awaiting_insurer: {"insurer_free": S.insurer_free, "insurer_paid": S.insurer_paid, "close": S.closed},
    S.insurer_free:     {"close": S.closed},
    S.insurer_paid:     {"hr_agreed": S.hr_agreed, "hr_declined": S.hr_declined, "close": S.closed},
    S.hr_agreed:        {"insurer_confirmed": S.insurer_confirmed, "close": S.closed},
    S.insurer_confirmed: {"close": S.closed},
    S.hr_declined:      {"close": S.closed},
    S.alt_found:        {"close": S.closed},
    S.insufficient:     {"close": S.closed},
    S.closed:           {},
}
ALL_ACTIONS = {a for t in TABLE.values() for a in t}


def test_transition_table_matches_config():
    for state, expected in TABLE.items():
        assert add_clinic.transitions(state) == expected, state
    assert set(TABLE) == set(S)


# ------------------------------------------------------------------------------------------- начальное состояние

def test_initial_alt_found_when_clinic_in_program():
    r, _ = started("улыбка")                       # нечёткое имя: «Клиника «Улыбка»»
    a = r.automation
    assert a and a.state == S.alt_found
    h = a.history[0]
    assert [f["clinic_name"] for f in h["found"]] == ["Клиника «Улыбка»"]
    assert h["found"][0]["source"]["source_ref"] == "t0:r1"
    assert [x["clinic_name"] for x in h["alternatives"]] == ["Клиника «Дента-Плюс»"]   # clinics_pick той же программы
    assert r.drafts == []                          # для alt_found черновика нет


def test_initial_not_in_program_marks_other_program_not_available():
    r, _ = started("Жемчуг")
    a = r.automation
    assert a and a.state == S.not_in_program
    h = a.history[0]
    assert h["other_programs"] == ["Премиум"]
    assert "не считается доступной" in h["note"] and "«Премиум»" in h["note"]
    alts = {x["clinic_name"] for x in h["alternatives"]}
    assert alts == {"Клиника «Улыбка»", "Клиника «Дента-Плюс»"}       # только c01 / «Стандарт», не «Жемчуг»
    assert all(x["source"]["contract_id"] == "c01" for x in h["alternatives"])
    [d] = r.drafts
    assert d.to.value == "hr" and d.stage == S.not_in_program
    assert "Жемчуг" in d.subject and "«Стандарт»" in d.subject
    assert "Компания 1" in d.body and "№ Д-c01" in d.body and "стоматология" in d.body
    assert "— Клиника «Улыбка», г. Москва, ул. Тверская, д. 1, франшиза 10 %" in d.body
    assert "Медси" not in d.body                                     # клиника другого договора ГК (ADR 0005)
    no_promises(r)


def test_initial_not_in_program_other_contract_clinic_is_not_found():
    r, _ = started("Медси")                        # есть только в c02 → для c01 её нет нигде
    assert r.automation and r.automation.state == S.not_in_program
    assert r.automation.history[0]["other_programs"] == [] and "note" not in r.automation.history[0]


def test_initial_insufficient_without_facts():
    st = kit()
    cases = [
        (answer(contract_id=None), req(), "Жемчуг", "договор"),
        (answer(), req(), None, "клиника"),
        (answer(), Request(id="r1", chat_id="c", title="t"), "Жемчуг", "вид обращения"),
    ]
    for ans, r, clinic, missing in cases:
        a = add_clinic.start(r, ans, clinic=clinic, store=st)
        assert a.state == S.insufficient, missing
        assert missing in a.history[0]["missing"]
        assert r.drafts == []


def test_single_program_of_contract_is_used_when_not_given():
    # в карточке c01 одна программа «Стандарт» — это факт договора, а не догадка
    r = req()
    a = add_clinic.start(r, answer(program=None), clinic="Жемчуг", store=kit())
    assert a.state == S.not_in_program and a.history[0]["program"] == "Стандарт"


def test_insufficient_then_restart_with_facts():
    st = kit()
    r = Request(id="r1", chat_id="c", title="t")
    assert add_clinic.start(r, answer(), clinic="Жемчуг", store=st).state == S.insufficient
    r.slots["care_type"] = "стоматология"
    assert add_clinic.start(r, answer(), clinic="Жемчуг", store=st).state == S.not_in_program
    assert len(r.drafts) == 1


# ------------------------------------------------------------------------------------------- ветки

def test_branch_free():
    r, st = started()
    run(r, "hr_wants_add", st=st)
    assert [d.to.value for d in r.drafts] == ["hr", "insurer"]
    ins = r.drafts[1]
    assert ins.stage == S.hr_wants_add and "Жемчуг" in ins.subject and "address" in ins.unknown_fields
    assert FILL in ins.body
    run(r, "insurer_request_ready", st=st)
    added_before(r)
    run(r, "insurer_free", st=st, reply="Клинику добавим без доплаты.")
    a = r.automation
    assert a and a.state == S.insurer_free
    h = a.history[-1]
    assert h["insurer_reply"]["text"] == "Клинику добавим без доплаты." and h["added_in_request"] is True
    assert r.drafts[-1].stage == S.insurer_free and "добавлена" in r.drafts[-1].subject
    run(r, "close", st=st)
    assert a.state == S.closed
    assert len(r.drafts) == 3
    no_promises(r)


def added_before(r: Request) -> None:
    """До подтверждения СК нигде нет «добавлена»: ни отметки в history, ни слова в письмах (ревизия w3)."""
    a = r.automation
    assert a is not None
    for h in a.history:
        if h.get("to") not in {s.value for s in add_clinic.ADDED_STATES}:
            assert "added_in_request" not in h, h.get("to")
    for d in r.drafts:
        if d.stage not in add_clinic.ADDED_STATES:
            # «добавлен(а)» как итог; «добавление» (просьба добавить) допустимо
            assert not re.search(r"\bдобавлен[аоы]?\b", (d.subject + d.body).lower()), d.stage


def test_branch_paid_agreed_confirmed():
    r, st = started()
    terms = "Добавление возможно с доплатой 1500 руб. на сотрудника."
    run(r, "hr_wants_add", "insurer_request_ready", st=st)
    run(r, "insurer_paid", st=st, reply=terms)
    hr = r.drafts[-1]
    assert hr.stage == S.insurer_paid and f"«{terms}»" in hr.body and "insurer_terms" not in hr.unknown_fields
    assert r.automation and r.automation.history[-1]["insurer_reply"]["kind"] == "insurer_reply"
    assert "added_in_request" not in r.automation.history[-1]          # «платно» — ещё не добавлена
    run(r, "hr_agreed", st=st)
    # согласие HR — только письмо в СК с просьбой подтвердить; клиника ещё НЕ добавлена
    assert [d.to.value for d in r.drafts[-1:]] == ["insurer"] and len(r.drafts) == 4
    assert r.automation.state == S.hr_agreed and "added_in_request" not in r.automation.history[-1]
    added_before(r)
    confirm = "Подтверждаем добавление клиники в программу."
    run(r, "insurer_confirmed", st=st, reply=confirm)
    h = r.automation.history[-1]
    assert r.automation.state == S.insurer_confirmed and h["added_in_request"] is True
    assert h["insurer_reply"] == {"text": confirm, "kind": "insurer_reply",
                                  "source": "ответ страховой компании, вставлен менеджером"}
    last = r.drafts[-1]
    assert last.to.value == "hr" and last.stage == S.insurer_confirmed and "добавлена" in last.subject
    run(r, "close", st=st)
    assert r.automation.state == S.closed and len(r.drafts) == 5
    added_before(r)
    no_promises(r)


def test_insurer_reply_with_promise_is_not_copied_to_hr_letter():
    """Ответ СК с обещанием срока и гарантии в письмо HR не попадает: поле условий очищается («[ЗАПОЛНИТЬ]»),
    в history — пометка, какие фразы помешали; сам ответ СК сохранён как источник."""
    r, st = started()
    reply = "Добавим в течение трёх рабочих дней, гарантия качества. Доплата 900 руб."
    run(r, "hr_wants_add", "insurer_request_ready", st=st)
    run(r, "insurer_paid", st=st, reply=reply)
    hr = r.drafts[-1]
    assert hr.stage == S.insurer_paid
    assert "трёх рабочих" not in hr.body and "гарантия" not in hr.body and "900" not in hr.body
    assert FILL in hr.body and "insurer_terms" in hr.unknown_fields and "insurer_terms" not in hr.fields
    assert find_promises(hr.subject + "\n" + hr.body) == []
    h = r.automation.history[-1] if r.automation else {}
    assert h["insurer_reply"]["text"] == reply                         # источник — дословно
    [flag] = h["promise_fields"]
    assert flag["draft"] == hr.id and flag["field"] == "insurer_terms"
    assert flag["phrases"] == ["в течение трёх рабочих", "гарантия"]
    # второй ответ СК (подтверждение) с обещанием тоже не попадает в письмо HR «клиника добавлена»
    run(r, "hr_agreed", st=st)
    run(r, "insurer_confirmed", st=st, reply="Клиника будет добавлена завтра.")
    last = r.drafts[-1]
    assert "завтра" not in last.body and "будет добавлена" not in last.body
    no_promises(r)


def test_template_of_draft():
    r, st = started()
    run(r, "hr_wants_add", "insurer_request_ready", "insurer_paid", "hr_agreed", "insurer_confirmed", st=st)
    tids = [add_clinic.template_of(d) for d in r.drafts]
    assert tids == ["hr_no_clinic", "insurer_add_request", "hr_paid_terms", "insurer_confirm_paid", "hr_clinic_added"]


def test_branch_paid_declined():
    r, st = started()
    run(r, "hr_wants_add", "insurer_request_ready", st=st)
    run(r, "insurer_paid", st=st, reply=None)                           # текста нет → условия «[ЗАПОЛНИТЬ]»
    assert "insurer_terms" in r.drafts[-1].unknown_fields
    n = len(r.drafts)
    run(r, "hr_declined", st=st)
    assert r.automation and r.automation.state == S.hr_declined and len(r.drafts) == n
    assert not any(h.get("added_in_request") for h in r.automation.history)
    added_before(r)
    try:
        add_clinic.step(r, "insurer_confirmed", insurer_reply="Подтверждаем", store=st)
    except ValueError as e:
        assert "недопустимо" in str(e)
    else:
        raise AssertionError("после отказа HR подтверждение СК недопустимо")
    run(r, "close", st=st)
    assert r.automation.state == S.closed


def test_close_from_any_state():
    paths = {
        S.alt_found: ("Улыбка", ()),
        S.not_in_program: ("Жемчуг", ()),
        S.hr_wants_add: ("Жемчуг", ("hr_wants_add",)),
        S.awaiting_insurer: ("Жемчуг", ("hr_wants_add", "insurer_request_ready")),
        S.insurer_free: ("Жемчуг", ("hr_wants_add", "insurer_request_ready", "insurer_free")),
        S.insurer_paid: ("Жемчуг", ("hr_wants_add", "insurer_request_ready", "insurer_paid")),
        S.hr_agreed: ("Жемчуг", ("hr_wants_add", "insurer_request_ready", "insurer_paid", "hr_agreed")),
        S.insurer_confirmed: ("Жемчуг", ("hr_wants_add", "insurer_request_ready", "insurer_paid", "hr_agreed",
                                         "insurer_confirmed")),
        S.hr_declined: ("Жемчуг", ("hr_wants_add", "insurer_request_ready", "insurer_paid", "hr_declined")),
    }
    for state, (clinic, actions) in paths.items():
        r, st = started(clinic)
        run(r, *actions, st=st)
        assert r.automation and r.automation.state == state
        n = len(r.drafts)
        run(r, "close", st=st)
        assert r.automation.state == S.closed and len(r.drafts) == n, state
    r = Request(id="r1", chat_id="c", title="t")
    add_clinic.start(r, answer(), clinic=None, store=kit())
    add_clinic.step(r, "close")
    assert r.automation and r.automation.state == S.closed


# ------------------------------------------------------------------------------------------- недопустимые переходы

def test_invalid_transitions_raise_and_do_not_change_request():
    # для каждого достижимого состояния — все действия, которых нет в таблице
    reach = {
        S.not_in_program: (), S.hr_wants_add: ("hr_wants_add",),
        S.awaiting_insurer: ("hr_wants_add", "insurer_request_ready"),
        S.insurer_free: ("hr_wants_add", "insurer_request_ready", "insurer_free"),
        S.insurer_paid: ("hr_wants_add", "insurer_request_ready", "insurer_paid"),
        S.hr_agreed: ("hr_wants_add", "insurer_request_ready", "insurer_paid", "hr_agreed"),
        S.insurer_confirmed: ("hr_wants_add", "insurer_request_ready", "insurer_paid", "hr_agreed",
                              "insurer_confirmed"),
        S.closed: ("close",),
    }
    for state, actions in reach.items():
        r, st = started()
        run(r, *actions, st=st)
        last = actions[-1] if actions else None
        for bad in sorted(ALL_ACTIONS - set(TABLE[state]) - {last} | {"unknown_action"}):
            before = r.model_dump_json()
            try:
                add_clinic.step(r, bad, store=st)
            except ValueError as e:
                assert "недопустимо" in str(e)
            else:
                raise AssertionError(f"{state} --{bad}--> должно быть ValueError")
            assert r.model_dump_json() == before


def test_branch_only_by_manager_action():
    r, st = started()
    run(r, "hr_wants_add", st=st)
    # из hr_wants_add нельзя сразу «бесплатно/платно»: сначала запрос в СК готов
    for a in add_clinic.INSURER_ACTIONS:
        try:
            add_clinic.step(r, a, insurer_reply="ответ", store=st)
        except ValueError:
            pass
        else:
            raise AssertionError(a)
    # текст ответа СК без действия «бесплатно/платно» не принимается
    try:
        add_clinic.step(r, "insurer_request_ready", insurer_reply="Добавим бесплатно", store=st)
    except ValueError as e:
        assert "ответа СК" in str(e)
    else:
        raise AssertionError("insurer_reply без insurer_free/insurer_paid")
    assert r.automation and r.automation.state == S.hr_wants_add


def test_step_without_automation():
    try:
        add_clinic.step(req(), "close")
    except ValueError as e:
        assert "не запущен" in str(e)
    else:
        raise AssertionError


# ------------------------------------------------------------------------------------------- идемпотентность

def test_repeat_step_is_noop():
    r, st = started()
    for _ in range(3):
        run(r, "hr_wants_add", st=st)
    assert len(r.drafts) == 2 and r.automation and len(r.automation.history) == 2
    run(r, "insurer_request_ready", st=st)
    for _ in range(2):
        run(r, "insurer_paid", st=st, reply="Доплата 1000 руб.")
    assert len(r.drafts) == 3 and len(r.automation.history) == 4
    try:
        add_clinic.step(r, "insurer_paid", insurer_reply="Другой текст", store=st)
    except ValueError as e:
        assert "уже зафиксирован" in str(e)
    else:
        raise AssertionError
    for _ in range(2):
        run(r, "hr_agreed", st=st)
    assert len(r.drafts) == 4 and len(r.automation.history) == 5
    for _ in range(2):
        run(r, "insurer_confirmed", st=st, reply="Подтверждаем.")
    run(r, "close", st=st)
    run(r, "close", st=st)
    assert len(r.drafts) == 5 and len(r.automation.history) == 7
    assert len({d.id for d in r.drafts}) == len(r.drafts)


def test_repeat_start_is_noop_and_other_clinic_blocked_while_in_progress():
    r, st = started()
    first = r.model_dump_json()
    a = add_clinic.start(r, answer(), clinic="«Жемчуг»", store=st)        # та же клиника (нормализация имени)
    assert a.state == S.not_in_program and r.model_dump_json() == first
    run(r, "hr_wants_add", st=st)
    try:
        add_clinic.start(r, answer(), clinic="Медси", store=st)
    except ValueError as e:
        assert "уже идёт" in str(e)
    else:
        raise AssertionError
    run(r, "close", st=st)
    a = add_clinic.start(r, answer(), clinic="Медси", store=st)          # после закрытия — новая клиника
    assert a.clinic == "Медси" and a.state == S.not_in_program
    assert len({d.id for d in r.drafts}) == len(r.drafts) == 3


def test_draft_ids_deterministic_by_request_and_clinic():
    r1, _ = started()
    r2, _ = started()
    assert [d.id for d in r1.drafts] == [d.id for d in r2.drafts]
    r3 = req()
    r3.id = "r2"
    add_clinic.start(r3, answer(), clinic="Жемчуг", store=kit())
    assert r3.drafts[0].id != r1.drafts[0].id


def test_history_has_no_dates_in_future_and_no_document_texts():
    r, st = started()
    run(r, "hr_wants_add", "insurer_request_ready", st=st)
    for h in r.automation.history if r.automation else []:
        assert dt.datetime.fromisoformat(h["at"]) <= dt.datetime.now(dt.UTC)
        assert "text" not in h                     # тексты пунктов документов в history не копируются
