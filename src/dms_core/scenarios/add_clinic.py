"""Автомат добавления клиники (ТЗ ред. 2 п. 5.2.2, ADR 0004, CONTRACTS §8). Переходы — config/add_clinic.yaml.

Начальное состояние определяет код, не модель:
- нет договора, программы, клиники или вида обращения → `insufficient` (черновиков нет, в history — чего не хватает);
- клиника найдена инструментом clinics_query в перечне ЭТОЙ программы с этим видом помощи → `alt_found`
  (варианты — сама клиника и clinics_pick той же программы);
- не найдена → `not_in_program`; если она есть в другой программе того же договора — пометка «есть в программе X —
  не считается доступной» (ADR 0005: доступной клиника другой программы не считается); черновик HR.
Переходы — только действием менеджера (ключи `transitions` конфига) плюс `close` с любого незакрытого шага.
Ветку «бесплатно / платно» задаёт только действие insurer_free / insurer_paid (ответ СК, ADR 0004); текст ответа СК
сохраняется в history как источник и цитатой идёт в письмо HR при «платно». Ветка «платно» доходит до «добавлена»
только через insurer_confirmed — второй ответ СК после согласия HR (ТЗ п. 5.2.2, ревизия w3). Клиника «добавлена» —
только отметка в запросе (history.added_in_request) в insurer_free / insurer_confirmed; документы договора не
меняются. Подставленное поле с обещанием (сроки, «гарантия») в письмо не идёт: «[ЗАПОЛНИТЬ]» и пометка
history.promise_fields (какое поле письма, какие фразы) — менеджер пишет условия своими словами.
Идемпотентность (ключ request_id + clinic + состояние): повтор последнего действия — без изменений; черновик
этапа имеет детерминированный id (drafts.draft_id) и второй раз не добавляется.
"""
from __future__ import annotations

import datetime as dt
from typing import Any

from ..config import load_yaml
from ..constants import AddClinicState
from ..schemas import Alternative, Answer, Automation, ClinicRow, Draft, Request
from ..store.db import program_matches
from ..tools._common import default_store
from ..tools.clinics import clinics_query, normalize_name
from . import clinics_pick, drafts

CLOSE = "close"
BRANCH_ACTIONS = ("insurer_free", "insurer_paid")   # ответ СК, который задаёт ветку (ADR 0004)
# Действия, которые фиксируют текст ответа СК: ветка и подтверждение добавления после согласия HR.
INSURER_ACTIONS = (*BRANCH_ACTIONS, "insurer_confirmed")
# Клиника отмечена добавленной в запросе только после ответа СК: «бесплатно» или подтверждение при «платно».
ADDED_STATES = (AddClinicState.insurer_free, AddClinicState.insurer_confirmed)


def config() -> dict[str, Any]:
    return load_yaml("add_clinic.yaml") or {}


def transitions(state: AddClinicState | str) -> dict[str, AddClinicState]:
    """Допустимые действия из состояния: из конфига + `close` с любого незакрытого шага (бриф w3: менеджер может
    закрыть запрос на любом шаге; в конфиге `close` перечислен не у всех состояний — см. отчёт этапа)."""
    st = AddClinicState(state)
    if st == AddClinicState.closed:
        return {}
    raw = (config().get("transitions") or {}).get(st.value) or {}
    out = {str(a): AddClinicState(t) for a, t in raw.items()}
    out.setdefault(CLOSE, AddClinicState.closed)
    return out


def _now() -> str:
    return dt.datetime.now(dt.UTC).isoformat()


def _clinic_key(clinic: str | None) -> str:
    return normalize_name(clinic or "")


def _row_brief(r: ClinicRow) -> dict[str, Any]:
    """Строка перечня для history/интерфейса — с источником (документ + строка таблицы)."""
    return {"clinic_name": r.clinic_name, "address": r.address, "program": r.program, "care_type": r.care_type,
            "franchise_pct": r.franchise_pct, "by_referral": r.by_referral, "access_note": r.access_note,
            "source": {"doc_id": r.doc_id, "contract_id": r.contract_id, "source_ref": r.source_ref, "row_id": r.id}}


def _alt_brief(a: Alternative) -> dict[str, Any]:
    return {**_row_brief(a.clinic), "reason": a.reason}


def _make_drafts(request: Request, clinic: str | None, state: AddClinicState, fields: dict[str, str],
                 flagged: list[dict[str, Any]] | None = None) -> list[Draft]:
    """Черновики этапа по config/add_clinic.yaml `drafts`; уже существующие (тот же id) не создаются заново.
    flagged — сюда дописываются поля, очищенные из-за обещания (drafts.promise_fields): пометка для менеджера."""
    have = {d.id for d in request.drafts}
    out = []
    for tid in (config().get("drafts") or {}).get(state.value, []) or []:
        did = drafts.draft_id(request.id, _clinic_key(clinic), state, tid)
        if did in have:
            continue
        if flagged is not None:
            for name, phrases in drafts.promise_fields(tid, fields).items():
                flagged.append({"draft": did, "field": name, "phrases": phrases})
        out.append(drafts.render(tid, fields, stage=state, draft_id=did))
    return out


def template_of(draft: Draft) -> str | None:
    """Шаблон черновика по этапу и адресату (config/add_clinic.yaml `drafts` + `to` шаблона) — нужен проверке
    обещаний после правки менеджера: allow_in действует только для своего шаблона (forbidden_promises.yaml)."""
    tpls = drafts.templates()
    for tid in (config().get("drafts") or {}).get(draft.stage.value, []) or []:
        if str((tpls.get(tid) or {}).get("to", "hr")) == draft.to.value:
            return str(tid)
    return None


# ---------------------------------------------------------------------------------------------- начальное состояние

def _missing(contract_id: str | None, program: str | None, clinic: str | None, care_type: str | None) -> list[str]:
    names = {"договор": contract_id, "программа": program, "клиника": clinic, "вид обращения": care_type}
    return [k for k, v in names.items() if not v]


def start(request: Request, answer: Answer | None, *, clinic: str | None, store: Any = None) -> Automation:
    """Запуск автомата по действию менеджера. Повторный запуск с той же клиникой (кроме `insufficient`) возвращает
    существующий автомат без новых черновиков; другая клиника — только если текущий автомат не в работе."""
    clinic = (clinic or request.slots.get("clinic") or "").strip() or None
    cur = request.automation
    if cur is not None:
        same = _clinic_key(cur.clinic) == _clinic_key(clinic)
        if same and cur.state != AddClinicState.insufficient:
            return cur                                                    # идемпотентность запуска
        initial = {AddClinicState(s) for s in config().get("initial", [])} | {AddClinicState.closed}
        if not same and cur.state not in initial:
            raise ValueError(f"в запросе уже идёт добавление клиники «{cur.clinic}» (шаг {cur.state.value}); "
                             "закройте его, прежде чем начинать новое")

    st = store or default_store()
    ctx = answer.context if answer is not None else None
    contract_id = (ctx.contract_id if ctx else None) or request.slots.get("contract_id")
    fields = drafts.draft_fields(answer, request, st)
    if clinic:
        fields["clinic"] = clinic
    program = (ctx.program if ctx else None) or request.slots.get("program")
    if not program and fields.get("program"):
        program = fields["program"].strip("«»")                         # единственная программа договора
    care_type = (request.slots.get("care_type") or request.slots.get("service") or "").strip() or None

    entry: dict[str, Any] = {"at": _now(), "action": "start", "from": None, "clinic": clinic,
                             "contract_id": contract_id, "program": program, "care_type": care_type}
    missing = _missing(contract_id, program, clinic, care_type)
    if missing:
        state = AddClinicState.insufficient
        entry["missing"] = missing
        new_drafts: list[Draft] = []
    else:
        assert contract_id and program and clinic and care_type
        state, info, alts = _initial_state(contract_id, program, clinic, care_type, answer, st)
        entry.update(info)
        if alts:
            fields["alternatives"] = drafts.format_alternatives(alts)
        auto = Automation(state=state, clinic=clinic)
        tmp = request.model_copy(update={"automation": auto})
        flagged: list[dict[str, Any]] = []
        new_drafts = _make_drafts(tmp, clinic, state, fields, flagged)
        if flagged:
            entry["promise_fields"] = flagged
    entry["to"] = state.value
    entry["fields"] = fields                    # базовые поля писем следующих шагов (без текстов документов)
    entry["drafts"] = [d.id for d in new_drafts]
    request.automation = Automation(state=state, clinic=clinic, history=[entry])
    request.drafts.extend(new_drafts)
    return request.automation


def _initial_state(contract_id: str, program: str, clinic: str, care_type: str, answer: Answer | None,
                   store: Any) -> tuple[AddClinicState, dict[str, Any], list[Alternative]]:
    """Есть ли клиника в перечне программы (clinics_query, нечёткое имя) — и что предложить взамен."""
    in_program = clinics_query(contract_id, program=program, care_type=care_type, name=clinic, store=store)["rows"]
    if in_program:
        rows = [r for r in store.clinics(contract_id, program=program)
                if r.id in {x["source"]["row_id"] for x in in_program}]
        alts = clinics_pick.pick(contract_id, program, care_type, store=store, exclude=clinic)
        return (AddClinicState.alt_found,
                {"found": [_row_brief(r) for r in rows], "alternatives": [_alt_brief(a) for a in alts]}, alts)

    # Клиники в программе нет. Есть ли она в другой программе ЭТОГО договора — только как пометка.
    anywhere = clinics_query(contract_id, name=clinic, store=store)["rows"]
    others = sorted({r["program"] for r in anywhere if not program_matches(r["program"], program)})
    # Альтернативы: из ответа (только тот же договор и программа — ADR 0005), иначе подбор clinics_pick.
    alts = [a for a in (answer.alternatives if answer else [])
            if a.clinic.contract_id == contract_id and program_matches(a.clinic.program, program)]
    if not alts:
        alts = clinics_pick.pick(contract_id, program, care_type, store=store, exclude=clinic)
    info: dict[str, Any] = {"alternatives": [_alt_brief(a) for a in alts], "other_programs": others}
    if others:
        info["note"] = (f"есть в программе {', '.join('«' + p + '»' for p in others)} — "
                        "не считается доступной по программе запроса")
    return AddClinicState.not_in_program, info, alts


# ---------------------------------------------------------------------------------------------- шаг автомата

def step(request: Request, action: str, *, insurer_reply: str | None = None, store: Any = None) -> Request:
    """Переход по действию менеджера. Недопустимое действие → ValueError (запрос не меняется)."""
    auto = request.automation
    if auto is None:
        raise ValueError("автомат добавления клиники не запущен для этого запроса")
    reply = (insurer_reply or "").strip() or None
    if reply and action not in INSURER_ACTIONS:
        raise ValueError("текст ответа СК фиксируется только действием «бесплатно», «платно» или "
                         "«СК подтвердила добавление»")

    last = auto.history[-1] if auto.history else {}
    if last.get("action") == action and last.get("to") == auto.state.value:
        # Повтор того же шага (двойной клик, повтор запроса) — ничего не меняем (идемпотентность).
        prev = (last.get("insurer_reply") or {}).get("text")
        if reply and prev != reply:
            raise ValueError("ответ СК на этом шаге уже зафиксирован; исправьте текст в черновике")
        return request

    allowed = transitions(auto.state)
    if action not in allowed:
        ok = ", ".join(allowed) or "нет (запрос закрыт)"
        raise ValueError(f"действие «{action}» недопустимо на шаге {auto.state.value}; допустимы: {ok}")
    target = allowed[action]

    fields = dict((auto.history[0].get("fields") if auto.history else None) or {})
    entry: dict[str, Any] = {"at": _now(), "action": action, "from": auto.state.value, "to": target.value}
    if action in INSURER_ACTIONS:
        # Ответ СК — источник решения ветки или подтверждения (ADR 0004); сохраняется как есть. В письмо HR — только
        # условия при «платно» и только без обещаний (drafts.render: поле с обещанием → «[ЗАПОЛНИТЬ]»).
        entry["insurer_reply"] = {"text": reply, "kind": "insurer_reply",
                                  "source": "ответ страховой компании, вставлен менеджером"} if reply else None
    # Условия СК для письма HR — текст ответа при «платно» (текущий шаг или зафиксированный ранее).
    paid = next((h.get("insurer_reply") for h in auto.history if h.get("action") == "insurer_paid"), None) or {}
    terms = reply if action == "insurer_paid" else paid.get("text")
    if terms:
        fields["insurer_terms"] = f"«{terms}»"

    flagged: list[dict[str, Any]] = []
    new_drafts = _make_drafts(request, auto.clinic, target, fields, flagged)    # рендер до изменения запроса
    if flagged:
        # Пометка менеджеру: поле письма очищено, потому что в подставленном тексте (обычно ответ СК) есть
        # обещание — срок, гарантия, «будет добавлена». Сам ответ СК остаётся в history как источник.
        entry["promise_fields"] = flagged
    if target in ADDED_STATES:
        entry["added_in_request"] = True       # только отметка в запросе; документы договора не меняются
    entry["drafts"] = [d.id for d in new_drafts]
    auto.history.append(entry)
    auto.state = target
    request.drafts.extend(new_drafts)
    return request
