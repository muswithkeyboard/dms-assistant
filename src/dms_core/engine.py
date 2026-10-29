"""Точка входа движка: вопрос + контекст чата → Answer (план §4 «Поток», docs/CONTRACTS.md §7).

Порядок:
  1. route — сценарий и слоты (1 вызов модели; без модели — правила по словам);
  2. clarify — нет обязательного факта (config/required_facts.yaml) → вопрос вместо догадки (`docs/rules.md` п. 2);
  3. договоры контекста: контекст «договор» — один; «ГК» — каждый договор группы отдельно (ADR 0005), параллельно;
  4. для каждого договора — run_agent с инструментами ТОЛЬКО этого договора → citations.verify (ссылка жива, только
     если её вернул инструмент в этом прогоне и цитата есть в пункте);
  5. SC 01 — метка кодом по config/verdict_rules.yaml (decide_label), не моделью;
  6. compose — форма ТЗ п. 8 и статус уверенности п. 8.1.
Без модели — режим extractive: найденные пункты и строки клиник без вывода, статус «данных недостаточно».
"""
from __future__ import annotations

import contextlib
import logging
import re
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from .constants import CLARIFY_OPTIONS_MAX, AnswerMode, Confidence, Label, Scenario, StatementKind
from .schemas import Answer, AnswerContext, ChatContext, ChatReply, Citation, LlmInfo, PerContract, Statement

log = logging.getLogger(__name__)
_PROGRAM_Q = re.compile(r"программ", re.IGNORECASE)   # уточняющий вопрос о программе (required_facts.yaml)

MAX_PARALLEL_CONTRACTS = 5     # ГК 3 — 5 договоров: все параллельно (план §14: p95 контекста ГК ≤ 45 с)


def _store(store: Any) -> Any:
    if store is not None:
        return store
    from .tools._common import default_store

    return default_store()


def contracts_of(context: ChatContext, store: Any) -> list[str]:
    """Договоры контекста чата. Контекст «договор» — ровно он; «ГК» — все договоры группы, а если в чате выбрана
    программа — только договоры, где она есть: застрахованный с этой программой — сотрудник этих юрлиц, и спрашивать
    программу по остальным договорам незачем (замечание пользователя 29.10.2026). Программы нет ни в одном договоре
    группы — все договоры, как раньше (программа уточнится в ответе)."""
    if context.contract_id:
        store.contract(context.contract_id)          # KeyError, если договор не загружен
        return [context.contract_id]
    if context.group_id:
        cards = store.contracts(context.group_id)
        ids = [c.id for c in cards]
        if not ids:
            raise KeyError(f"в группе {context.group_id} нет загруженных договоров")
        if context.program:
            want = _norm(context.program)
            own = [c.id for c in cards if any(_norm(p.name) == want for p in (c.programs or []))]
            if own:
                return own
        return ids
    raise ValueError("контекст чата не выбран: нужна группа компаний или договор")


def _norm(s: str) -> str:
    return " ".join((s or "").casefold().replace("ё", "е").replace("№", " ").split())


def resolve_contract_slot(value: str | None, context: ChatContext, store: Any) -> str | None:
    """Слот «договор» в контексте ГК → contract_id, только если он однозначно указывает на договор группы:
    точное юрлицо («Компания 2»), номер договора («№ 2», «2») или id («c02»). Иначе None — тогда остаётся вопрос
    «какой договор?» (ревизия w2, дефект 8: общая лемма «компания» не должна закрывать уточнение)."""
    if not value or context.contract_id or not context.group_id:
        return None
    v = _norm(value)
    hits = []
    for c in store.contracts(context.group_id):
        names = {_norm(c.legal_entity), _norm(c.id), _norm(c.number or "")} - {""}
        number = (c.number or "").strip()
        if v in names or (number and v in {f"договор {_norm(number)}", _norm(number)}):
            hits.append(c.id)
    return hits[0] if len(hits) == 1 else None


def program_for(cid: str, program: str | None, store: Any) -> str | None:
    """Программа контекста/слота уходит в агента договора, только если у договора такая программа есть:
    программа одного договора не переносится на другие договоры ГК (ADR 0005)."""
    if not program:
        return None
    names = {_norm(p.name) for p in store.contract(cid).programs}
    return program if _norm(program) in names else None


def _answer_context(context: ChatContext) -> AnswerContext:
    return AnswerContext(group_id=context.group_id, contract_id=context.contract_id, program=context.program,
                         date=context.date)


# ------------------------------------------------------------------ режим без модели

# Сценарии, в которых ответ предлагает запуск автомата добавления клиники (кнопка в интерфейсе, FR 09:
# информационный вопрос автомат не запускает — только предлагает)
CLINIC_SCENARIOS = (Scenario.sc02_add_clinic, Scenario.sc02_clinic)


def suggest_automation(scenario: Scenario, slots: dict) -> str | None:
    """Кнопка «Добавить клинику» — в сценариях с клиникой и всегда, когда в обращении названа клиника."""
    if scenario in CLINIC_SCENARIOS or slots.get("clinic"):
        return "add_clinic"
    return None


def _titles(store: Any, cid: str) -> dict[str, str]:
    return {d.id: d.title for d in store.documents(cid)}


def extractive(question: str, context: ChatContext, scenario: Scenario, store: Any, k: int = 5,
               slots: dict | None = None) -> Answer:
    """Без модели: найденные пункты по каждому договору, без вывода и без метки (план §6.4); для сценариев
    с клиникой — альтернативы кодом (clinics_pick, допущение A6), если известны программа и вид помощи."""
    from .scenarios.clinics_pick import pick
    from .tools import search_clauses

    slots = slots or {}
    per: list[PerContract] = []
    alternatives = []
    for cid in contracts_of(context, store):
        titles = _titles(store, cid)
        hits = search_clauses(cid, question, k=k, program=context.program, store=store)["hits"]
        statements = [
            Statement(text=h["text"][:400], kind=StatementKind.fact, citations=[Citation(
                doc_id=h["source"]["doc_id"], doc_title=titles.get(h["source"]["doc_id"], h["source"]["doc_id"]),
                contract_id=cid, clause_number=h["number"] or (h.get("path") or "").split(" › ")[-1] or None,
                quote=h["text"][:200], verified=True)])
            for h in hits
        ]
        per.append(PerContract(contract_id=cid, summary="Найденные пункты (без вывода модели)",
                               statements=statements))
        program = program_for(cid, context.program or slots.get("program"), store)
        care = slots.get("care_type") or slots.get("service")
        if (scenario in (*CLINIC_SCENARIOS, Scenario.sc01) or slots.get("clinic")) and program and care:
            alternatives += pick(cid, program, care, near=slots.get("city"), store=store,
                                 exclude=slots.get("clinic"))
    return Answer(
        scenario=scenario, context=_answer_context(context), confidence=Confidence.insufficient,
        summary="Модель недоступна: показаны найденные пункты документов без вывода — проверьте вручную.",
        per_contract=per if len(per) > 1 else [],
        statements=per[0].statements if len(per) == 1 else [],
        alternatives=alternatives, suggested_automation=suggest_automation(scenario, slots),
        mode=AnswerMode.extractive,
    )


# ------------------------------------------------------------------ основной путь

def _one_contract(llm: Any, cid: str, question: str, route: Any, context: ChatContext, store: Any,
                  mode: str = "agent", clinic_pick: str | None = None) -> dict:
    """Один договор → черновик (агент с моделью или guided — кодом, ADR 0009) → проверка ссылок → метка (SC 01).
    Ничего из других договоров сюда не попадает."""
    from .citations import verify
    from .scenarios.sc01_refusal import decide_label

    given = context.program or route.slots.get("program")
    program = program_for(cid, given, store)
    slots = {k: v for k, v in route.slots.items() if k not in ("program", "contract")}
    if program:
        slots["program"] = program
    elif given:
        # программа задана, но у этого договора такой нет — вопрос о программе скажет об этом прямо (ADR 0019)
        slots["program_not_found"] = str(given)
    if clinic_pick:
        # выбор менеджера (ADR 0010): id строки перечня этого договора или "none" — guided проверяет и учитывает
        slots["clinic_pick"] = clinic_pick
    ctx = context.model_copy(update={"contract_id": cid, "program": program})
    if mode == "guided":
        # Ответ ведёт код: инструменты договора вызывает guided по сценарию; модель — только краткий вывод по флагу
        from .guided import run_guided

        run = run_guided(cid, question, scenario=route.scenario, slots=slots, context=ctx, store=store, llm=llm)
    else:
        from .agent import run_agent

        run = run_agent(llm, cid, question, scenario=route.scenario, slots=slots, context=ctx, store=store)
    # run.refs — ссылки прогона вместе с записями инструментов (нужны для absent и ссылок на вычисления)
    verified = verify(run.draft, run.refs, store, cid, program=program)
    label = note = rule = None
    if route.scenario == Scenario.sc01 and verified.draft.checks is not None:
        if not verified.citations:
            # Ни одной живой ссылки — вывода нет: «недостаточно данных», а не метка из проверок модели
            # (`docs/rules.md` п. 1; ревизия w2, дефект 1 — вторая линия защиты после verify)
            label, note, rule = Label.insufficient, None, "no_evidence"
        else:
            # клиника подтверждена, только если guided учёл выбор менеджера (ADR 0010); агент выбора не учитывает
            choice = verified.draft.clinic_choice
            confirmed = (mode == "guided" and bool(clinic_pick) and choice is not None
                         and choice.selected == clinic_pick)
            label, note, rule = decide_label(verified.draft.checks,
                                             has_alternative=bool(verified.draft.alternatives),
                                             facts={"refusal": bool(route.slots.get("refusal")),
                                                    "clinic_confirmed": confirmed,
                                                    # клиника названа и guided её разбирал (ложный слот, который
                                                    # guided отбросил, не делает ответ «выберите клинику»)
                                                    "clinic_named": choice is not None})
    return {"contract_id": cid, "run": run, "verified": verified, "label": label, "label_note": note, "rule": rule}


def _draft_refs(draft: Any) -> list[str]:
    """Все ref черновика модели до фильтра (для метрики «ссылки на чужой договор до фильтра», план §10.1)."""
    refs = [c.ref for st in draft.statements for c in st.citations]
    refs += [a.row_id for a in draft.alternatives]
    refs += [x.ref for c in draft.contradictions for x in (c.a, c.b)]
    return refs


def _fill_trace(trace: dict, results: list[dict]) -> None:
    """След прогона для eval (CONTRACTS §7): без текстов документов — только счётчики и ref."""
    trace["dropped"] = sum(len(x["verified"].dropped) for x in results)
    trace["kept"] = sum(len(x["verified"].citations) for x in results)
    trace["draft_refs"] = sorted({r for x in results for r in _draft_refs(x["run"].draft)})
    trace["run_refs"] = sorted({r for x in results for t in x["run"].tools for r in t.refs})
    trace.setdefault("automation_states", [])
    trace["per_contract"] = {x["contract_id"]: {"rule": x["rule"], "steps": x["run"].steps,
                                                "error": x["run"].error} for x in results}


def answer(question: str, context: ChatContext, *, llm: Any = None, store: Any = None,
           trace: dict | None = None, on_stage: Callable[[str], None] | None = None,
           clinic_picks: dict[str, str] | None = None, _assisted: bool = False) -> Answer:
    """trace — необязательный словарь, который движок заполняет следом прогона (eval/runner.py);
    on_stage — обратный вызов стадий подготовки ответа для индикатора в интерфейсе (FR 17): route,
    clarify, contract:<id>, verify:<id>, compose. Сбой обратного вызова ответ не ломает.
    clinic_picks — выбор клиники менеджером по договорам {contract_id: row_id | "none"} (ADR 0010)."""
    def stage(name: str) -> None:
        if on_stage is not None:
            with contextlib.suppress(Exception):   # индикатор не должен ронять ответ
                on_stage(name)

    t0 = time.perf_counter()
    stage("route")
    store = _store(store)
    from .inputcheck import is_gibberish, smalltalk, wrong_layout

    talk = smalltalk(question)
    if talk is not None:
        return _not_understood(question, context, None, llm, t0, lead=talk)
    fixed = wrong_layout(question)
    if fixed is not None or is_gibberish(question):
        # не вопрос (случайный набор) или английская раскладка — по документам не ищется (круг 2 ревизии w16)
        return _not_understood(question, context, fixed, llm, t0)
    from .guided import choose_scenario, engine_mode, fill_slots, llm_slots_enabled
    from .scenarios.clarify import split_missing
    from .scenarios.route import route as do_route

    # Режим (ADR 0009): guided — без модели или с «малой» моделью; agent — сильная модель; extractive — запас
    mode = engine_mode(llm)
    if mode == "guided":
        # сценарий — правила + уверенный kNN по эмбеддингам (route.py); слоты моделью — только по флагу
        r = choose_scenario(question, context, store=store)
        if llm is not None and llm_slots_enabled():
            r.slots = fill_slots(question, r.scenario, r.slots, context, llm, store=store)
    else:
        r = do_route(question, context, llm=llm, store=store)
    understood = None
    if mode == "guided" and not _assisted and _weak_route(r):
        # правила вопрос не поняли — модель-помощник переписывает его в стандартный вид (assist.py, w17); берётся,
        # только если маршрут стал лучше и не потерял клинику / программу / договор из исходного вопроса
        from .assist import enabled, rephrase

        if enabled(llm):
            stage("understand")
            alt = rephrase(question, llm)
            if alt:
                r2 = choose_scenario(alt, context, store=store)
                if not _weak_route(r2) and all(normalize_slot(r.slots.get(k)) in ("", normalize_slot(r2.slots.get(k)))
                                               for k in ("clinic", "program", "contract")):
                    r, question, understood = r2, alt, alt
    narrowed = resolve_contract_slot(r.slots.get("contract"), context, store)
    if context.group_id and not context.contract_id and not narrowed:
        r.slots.pop("contract", None)        # неоднозначный слот не закрывает вопрос «какой договор?»
    if narrowed:
        context = context.model_copy(update={"contract_id": narrowed})
    stage("clarify")
    if trace is not None:
        # слоты маршрутизации — для запроса (автомат добавления клиники берёт из них клинику и вид помощи)
        trace["scenario"] = r.scenario.value
        trace["slots"] = dict(r.slots)
    # Вопросы пересчитываются после поправки слотов. Блокирующие (без факта нет вывода) — вопрос вместо ответа;
    # остальные — ответ даётся, недостающее — в «что неизвестно» (config/required_facts.yaml, blocking).
    # ответ на «Какая программа?», которого нет среди программ («омс»): не слот клиники и не тот же вопрос молча
    from .followup import glued_reply

    glued = glued_reply(question)
    unknown_program = None
    if glued and _PROGRAM_Q.search(glued[0]) and not r.slots.get("program") and not context.program:
        unknown_program = glued[1]
        if normalize_slot(r.slots.get("clinic")) == normalize_slot(unknown_program):
            r.slots.pop("clinic", None)
            r.slots.pop("clinic_exact", None)
    questions, advisory = split_missing(r.scenario, r.slots, context, store=store)
    if mode == "extractive":
        out = extractive(question, context, r.scenario, store, slots=r.slots)
        out.questions = questions + advisory
        out.reply = _reply(out, question, store)
        out.processing_ms = int(1000 * (time.perf_counter() - t0))
        return out
    if questions:
        # Нет обязательного факта — уточняющий вопрос вместо догадки (`docs/rules.md` п. 2)
        out = Answer(scenario=r.scenario, context=_answer_context(context), summary="Нужно уточнение.",
                     confidence=Confidence.need_clarification, questions=questions,
                     mode=AnswerMode.live if llm is not None else AnswerMode.guided)
        out.reply = _reply(out, question, store)
        if out.reply is not None:
            out.reply.understood = understood
        if out.reply is not None and not r.slots.get("program") and not context.program:
            # «Какая программа?» — варианты кнопками: названия программ договора (ГК — всех договоров), только
            # когда вопрос о программе задан (required_facts.yaml) и программ немного
            from .scenarios.route import _programs
            progs = _programs(context, store)
            if 1 < len(progs) <= CLARIFY_OPTIONS_MAX and any(_PROGRAM_Q.search(q) for q in questions):
                out.reply.options = progs
            if unknown_program and any(_PROGRAM_Q.search(q) for q in questions):
                from .compose import _reply_texts

                out.reply.lead = _reply_texts()["lead_unknown_program"].format(
                    text=unknown_program, question=out.reply.lead)
        out.processing_ms = int(1000 * (time.perf_counter() - t0))
        return out

    cids = contracts_of(context, store)
    with ThreadPoolExecutor(max_workers=min(MAX_PARALLEL_CONTRACTS, len(cids))) as pool:
        def run_one(c: str) -> dict:
            stage(f"contract:{c}")
            res = _one_contract(llm, c, question, r, context, store, mode, (clinic_picks or {}).get(c))
            stage(f"verify:{c}")
            return res

        results = list(pool.map(run_one, cids))

    from .compose import compose

    label: Label | None = None
    note: str | None = None
    if len(results) == 1:
        label, note = results[0]["label"], results[0]["label_note"]
    stage("compose")
    out = compose(r.scenario, _answer_context(context), {x["contract_id"]: x["verified"] for x in results},
                  question=question, store=store,
                  suggested_automation=suggest_automation(r.scenario, r.slots),
                  label=label, label_note=note,
                  labels={x["contract_id"]: x["label"] for x in results},
                  label_notes={x["contract_id"]: x["label_note"] for x in results})
    if trace is not None:
        _fill_trace(trace, results)
    calls = sum(len(x["run"].tools) for x in results)
    from .config import prompt_version

    # кандидаты клиники для выбора менеджером — по договорам, в порядке договоров (ADR 0010)
    out.clinic_choices = [x["verified"].draft.clinic_choice for x in results
                          if x["verified"].draft.clinic_choice is not None]
    for q in advisory:                      # ответ дан; недостающее — в «что неизвестно» с вопросом
        if q not in out.missing:
            out.missing.append(q)
    if llm is not None:
        out.llm = LlmInfo(provider=getattr(llm, "provider", "?"), model=getattr(llm, "model", "?"),
                          prompt_version=prompt_version("agent_system" if mode == "agent" else "guided_summary"),
                          tool_calls=calls)
        out.mode = AnswerMode.live
    else:
        # guided без модели: вывод и метку построил код по документам (режим guided, ADR 0009)
        out.llm = None
        out.mode = AnswerMode.guided
    # ответ в чате — после выбора клиники и неблокирующих уточнений (ADR 0014)
    out.reply = _reply(out, question, store)
    if out.reply is not None:
        out.reply.understood = understood
    if mode == "guided" and not _assisted and understood is None and not _found(out):
        # по вопросу в документах ничего не нашлось (правила взяли разговорное слово за услугу: «к лору») — один
        # повтор с вопросом, переписанным моделью-помощником; берётся, только если нашлись пункты (ADR 0015)
        from .assist import enabled, rephrase

        alt = rephrase(question, llm) if enabled(llm) else None
        if alt:
            again = answer(alt, context, llm=llm, store=store, trace=trace, on_stage=on_stage,
                           clinic_picks=clinic_picks, _assisted=True)
            if _found(again) and again.reply is not None:
                again.reply.understood = alt
                again.processing_ms = int(1000 * (time.perf_counter() - t0))
                return again
    out.processing_ms = int(1000 * (time.perf_counter() - t0))
    return out


def _found(out: Answer) -> bool:
    """В ответе есть хотя бы одно проверенное утверждение (по договору или по договорам группы)."""
    return bool(out.statements) or any(pc.statements for pc in out.per_contract)


def _weak_route(r: Any) -> bool:
    """Правила вопрос не поняли: общий сценарий или нет ни услуги, ни вида помощи, ни клиники."""
    return r.scenario == Scenario.general or not any(r.slots.get(k) for k in ("service", "care_type", "clinic"))


def normalize_slot(value: Any) -> str:
    return _norm(str(value or "")).strip("«»\"' ")


def _not_understood(question: str, context: ChatContext, fixed: str | None, llm: Any, t0: float,
                    lead: str | None = None) -> Answer:
    """Ответ на ввод, который не вопрос: реплика («привет» — lead из smalltalk), «не понял вопрос» с примером или, при
    английской раскладке, тот же вопрос по-русски кнопкой (reply.options). Документы не просматривались — уверенность «данных недостаточно»."""
    from .compose import _reply_texts, sentences

    t = _reply_texts()
    lead = lead or (t["lead_wrong_layout"].format(text=fixed) if fixed else t["lead_not_understood"])
    out = Answer(scenario=Scenario.general, context=_answer_context(context), summary=sentences(lead)[0],
                 confidence=Confidence.insufficient,
                 mode=AnswerMode.live if llm is not None else AnswerMode.guided)
    out.reply = ChatReply(lead=lead, options=[fixed] if fixed else [], service=True)
    out.processing_ms = int(1000 * (time.perf_counter() - t0))
    return out


def _reply(out: Answer, question: str, store: Any) -> Any:
    """Ответ словами (compose.chat_reply, ADR 0014). Сбой сборки текста не роняет ответ: остаётся прежний вид."""
    from .compose import chat_reply

    try:
        return chat_reply(out, question=question, store=store)
    except Exception:
        log.warning("chat_reply: сбой сборки ответа", exc_info=True)
        return None
