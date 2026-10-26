"""Клиника подтверждается менеджером (ADR 0010, CONTRACTS §8.1), без сети на синтетике: кандидаты из перечня
программы, выбор строки → «можно», «нет в перечне» → «нельзя», строка чужого договора/программы не учитывается,
агент выбор не учитывает, контекст ГК — выбор по одному договору не влияет на другой."""
from __future__ import annotations

from dms_core import engine
from dms_core.citations import verify
from dms_core.constants import CLINIC_CHOICE_MAX, CLINIC_PICK_NONE, Label, Scenario
from dms_core.guided import clinic_candidates, run_guided
from dms_core.schemas import CareMatrix, ChatContext, ClinicRow
from dms_core.store import KitStore
from fixtures.synth_kit import make_parsed
from guided.test_recall2 import CTX, EXCLUSIONS, INCLUDED, _kit
from helpers import label_as_engine, skip_unless_rule, with_monkeypatch

Q_OK = "Прием невролога в Ромашке здоровья, по ДМС пойдет?"
Q_NONE = "Прием невролога в клинике «Несуществующая Альфа», по ДМС пойдет?"


def _guided(st: KitStore, question: str, pick: str | None = None, cid: str = "c01"):
    """Слоты правилами (как движок без модели) → guided → verify → метка как в движке."""
    from dms_core.scenarios.route import rule_slots

    ctx = CTX.model_copy(update={"contract_id": cid})
    slots = {**rule_slots(question, ctx, st), "program": "Стандарт", **({"clinic_pick": pick} if pick else {})}
    run = run_guided(cid, question, scenario=Scenario.sc01, slots=slots, context=ctx, store=st)
    v = verify(run.draft, run.refs, st, cid, program="Стандарт", tools=run.tools)
    return v, label_as_engine(v, pick)[0]


# ---------------------------------------------------------------- кандидаты


def test_candidates_strict_first_care_and_city_first_no_duplicates_capped():
    p = make_parsed("c01")
    mk = [  # (id, программа, вид помощи, название, адрес, город)
        ("r0", "Стандарт", "Стоматологическая помощь", "Альтамед Север", "ул. Ленина, 1", "Казань"),
        ("r1", "Стандарт", "Амбулаторно-поликлиническая помощь", "Альтамед", "ул. Мира, 2", "Москва"),
        ("r2", "Стандарт", "Стоматологическая помощь", "Альтамед", "ул. Мира, 2", "Москва"),
        ("r3", "Стандарт", "Стоматологическая помощь", "Альтамед", "ул. Мира, 2", "Москва"),   # дубль r2
        ("r4", "Стандарт", "Стоматологическая помощь", "Альтамед", "пр. Победы, 9", "Казань"),
        ("r5", "Премиум", "Стоматологическая помощь", "Альтамед", "ул. Арбат, 3", "Москва"),   # чужая программа
        ("r6", "Стандарт", "Стоматологическая помощь", "Ромашка", "ул. Мира, 5", "Москва"),   # непохожая
    ]
    p.clinics = [ClinicRow(id=f"c01.clinics#{i}", contract_id="c01", doc_id="c01.clinics", program=prog,
                           care_type=ct, clinic_name=n, address=a, city=c, source_ref=i)
                 for i, prog, ct, n, a, c in mk]
    st = KitStore(":memory:")
    st.save(p)
    got = clinic_candidates(st, "c01", "Стандарт", "Альтамед Север", "Стоматологическая помощь", "стоматология",
                            city="Москва")
    ids = [r.id.split("#")[1] for r in got]
    # строгое совпадение («Альтамед Север», хоть и в другом городе) раньше нестрогих «Альтамед»; среди нестрогих —
    # стоматология в Москве, затем в Казани, затем другой вид помощи; дубль (клиника + адрес + вид помощи),
    # чужая программа и непохожая клиника не попадают
    assert ids == ["r0", "r2", "r4", "r1"]
    assert all(isinstance(r, ClinicRow) and r.program == "Стандарт" for r in got)
    # предел CLINIC_CHOICE_MAX
    p.clinics = [ClinicRow(id=f"c01.clinics#n{i}", contract_id="c01", doc_id="c01.clinics", program="Стандарт",
                           care_type="Стоматологическая помощь", clinic_name="Альтамед", address=f"ул. {i}",
                           source_ref=f"n{i}") for i in range(CLINIC_CHOICE_MAX + 4)]
    st2 = KitStore(":memory:")
    st2.save(p)
    assert len(clinic_candidates(st2, "c01", "Стандарт", "Альтамед", None, None)) == CLINIC_CHOICE_MAX


def test_choice_in_answer_without_pick_keeps_old_labels():
    st = _kit(INCLUDED + EXCLUSIONS)
    a = engine.answer(Q_OK, CTX, store=st)
    assert [c.contract_id for c in a.clinic_choices] == ["c01"]
    ch = a.clinic_choices[0]
    assert ch.program == "Стандарт" and ch.selected is None
    assert ch.candidates and ch.candidates[0].id == "c01.clinics#s0" and ch.candidates[0].clinic_name.endswith(
        "«Ромашка здоровья»")
    # без выбора клиника по тексту «можно» не даёт (ADR 0010)
    assert a.label is not Label.allowed
    # клиника не названа — выбора нет
    assert engine.answer("Прием невролога по ДМС пойдет?", CTX, store=st).clinic_choices == []


# ---------------------------------------------------------------- выбор строки и «нет в перечне»


def test_pick_row_gives_allowed_when_service_included_without_conditions():
    skip_unless_rule("covered")
    st = _kit(INCLUDED + EXCLUSIONS)
    a = engine.answer(Q_OK, CTX, store=st, clinic_picks={"c01": "c01.clinics#s0"})
    assert a.label is Label.allowed and a.clinic_choices[0].selected == "c01.clinics#s0"
    v, label = _guided(st, Q_OK, "c01.clinics#s0")
    backer = [s for s in v.draft.statements if "clinic_in_program" in s.supports]
    assert label is Label.allowed and backer and backer[0].citations[0].ref == "c01.clinics#s0"


def test_pick_row_with_other_care_is_not_found():
    # «Северская ОКБ» в перечне — стоматология; вопрос о неврологе → «не нашли» с видами помощи строки
    st = _kit(INCLUDED + EXCLUSIONS)
    v, label = _guided(st, "Прием невролога в Северской ОКБ, по ДМС пойдет?", "c01.clinics#s6")
    assert v.draft.checks.clinic_in_program == "not_found" and label is not Label.allowed
    assert v.draft.clinic_choice.selected == "c01.clinics#s6"
    assert any("Стоматологическая помощь" in s.text for s in v.draft.statements)


def test_pick_row_refusal_still_blocks_allowed():
    st = _kit(INCLUDED + EXCLUSIONS)
    a = engine.answer("В Ромашке здоровья сказали, что невролог по ДМС не принимает, только за свой счет", CTX,
                      store=st, clinic_picks={"c01": "c01.clinics#s0"})
    assert a.label is not Label.allowed


def test_pick_none_gives_denied_with_alternative():
    skip_unless_rule("clinic_absent")
    st = _kit(INCLUDED + EXCLUSIONS)
    a0 = engine.answer(Q_NONE, CTX, store=st)
    assert a0.label is not Label.denied and a0.clinic_choices and a0.clinic_choices[0].candidates == []
    a = engine.answer(Q_NONE, CTX, store=st, clinic_picks={"c01": CLINIC_PICK_NONE})
    assert a.label is Label.denied and a.clinic_choices[0].selected == CLINIC_PICK_NONE
    # альтернативы — строки той же программы с видом помощи вопроса
    assert a.alternatives and all(x.clinic.program == "Стандарт" for x in a.alternatives)


def test_pick_none_without_listing_is_not_found():
    p = make_parsed("c01")
    p.clinics = []
    st = KitStore(":memory:")
    st.save(p)
    v, label = _guided(st, Q_NONE, CLINIC_PICK_NONE)
    assert v.draft.checks.clinic_in_program == "not_found" and label is not Label.denied
    assert v.draft.clinic_choice.selected is None


def test_foreign_contract_or_program_row_is_not_counted():
    st = _kit(INCLUDED + EXCLUSIONS)
    # c02.* — строка другого договора; c01.clinics#s12 — строка программы «Премиум» этого договора
    for bad in ("c02.clinics#t0r0", "c01.clinics#s12", "c01.clinics#нет"):
        v, label = _guided(st, Q_OK, bad)
        assert v.draft.clinic_choice.selected is None, bad
        assert v.draft.checks.clinic_in_program == "not_found" and label not in (Label.allowed, Label.denied), bad
        a = engine.answer(Q_OK, CTX, store=st, clinic_picks={"c01": bad})
        assert a.label not in (Label.allowed, Label.denied), bad


# ---------------------------------------------------------------- агент и ГК


@with_monkeypatch
def test_agent_mode_never_confirms_clinic(mp):
    """Сильная модель (agent) выбора не учитывает: её черновик без clinic_choice — clinic_confirmed нет."""
    import dms_core.agent as agent_mod
    from dms_core.guided import choose_scenario

    st = _kit(INCLUDED + EXCLUSIONS)

    def fake_agent(llm, cid, question, *, scenario, slots, context, store):
        run = run_guided(cid, question, scenario=scenario, slots=slots, context=context, store=store)
        run.draft = run.draft.model_copy(update={"clinic_choice": None})   # агент кандидатов не собирает
        return run

    mp.setattr(agent_mod, "run_agent", fake_agent)
    r = choose_scenario(Q_OK, CTX, store=st)
    res = engine._one_contract(object(), "c01", Q_OK, r, CTX, st, "agent", "c01.clinics#s0")
    assert res["verified"].draft.checks.clinic_in_program == "yes"
    assert res["label"] is not Label.allowed
    # тот же черновик в guided с тем же выбором — «можно» (проверка, что дело именно в подтверждении)
    res_g = engine._one_contract(None, "c01", Q_OK, r, CTX, st, "guided", "c01.clinics#s0")
    assert res_g["label"] is Label.allowed


def _gk_store() -> KitStore:
    """Два договора ГК с одинаковой клиникой в перечне (договоры ГК совпадают на 87–99 %)."""
    st = _kit(INCLUDED + EXCLUSIONS)
    p2 = make_parsed("c02")
    p2.clauses += [c.model_copy(update={"id": c.id.replace("c01.", "c02."), "doc_id": c.doc_id.replace("c01.", "c02."),
                                        "contract_id": "c02"})
                   for c in st.clauses("c01") if c.id.startswith("c01.program#r")]
    p2.clinics = [r.model_copy(update={"id": r.id.replace("c01.", "c02."), "contract_id": "c02", "doc_id": "c02.clinics"})
                  for r in st.clinics("c01")]
    p2.care = [CareMatrix(contract_id="c02", program="Стандарт", care_type="outpatient", present=True,
                          source_refs=["m1"])]
    st.save(p2)
    return st


def test_group_context_pick_per_contract_only():
    st = _gk_store()
    gk = ChatContext(group_id="gk9", program="Стандарт")
    a0 = engine.answer(Q_OK, gk, store=st)
    assert {c.contract_id for c in a0.clinic_choices} == {"c01", "c02"}
    for ch in a0.clinic_choices:   # кандидаты — только строки своего договора
        assert all(r.contract_id == ch.contract_id for r in ch.candidates)
    a = engine.answer(Q_OK, gk, store=st, clinic_picks={"c01": "c01.clinics#s0"})
    by = {c.contract_id: c for c in a.clinic_choices}
    assert by["c01"].selected == "c01.clinics#s0" and by["c02"].selected is None
    labels = {p.contract_id: p.label for p in a.per_contract}
    assert labels["c02"] is not Label.allowed
    # строка c01 в выборе по c02 не принимается (ADR 0005)
    a2 = engine.answer(Q_OK, gk, store=st, clinic_picks={"c02": "c01.clinics#s0"})
    assert {c.contract_id: c.selected for c in a2.clinic_choices}["c02"] is None
    assert {p.contract_id: p.label for p in a2.per_contract}["c02"] is not Label.allowed
