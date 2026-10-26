"""Состояния автомата добавления клиники в эталонах → AddClinicState (ADR 0004, config/add_clinic.yaml).

Аналитики своих кейсов (own_gk*.jsonl) писали шаги процесса своими словами: «clinic_added», «hr_approval», …
Файлы аналитиков не правим (бриф w2-eval п. 4) — сводим здесь словарём соответствий, а затем достраиваем
неявные шаги по переходам config/add_clinic.yaml (hr_wants_add всегда идёт через awaiting_insurer).
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence

from dms_core.constants import AddClinicState as S

# Слово аналитика → состояние автомата. None — шаг не является состоянием (действие внутри состояния), пропускается.
ALIASES: dict[str, str | None] = {
    # «HR уведомлён, что клиника добавлена» / «клиника добавлена» — черновик HR в insurer_free/hr_agreed, затем закрыть
    "hr_notified_added": S.closed,
    "clinic_added": S.closed,
    # «согласование у HR» в платной ветке — итог действия «HR согласен» (insurer_paid → hr_agreed)
    "hr_approval": S.hr_agreed,
    # «подтверждение в СК» — черновик insurer_confirm_paid внутри hr_agreed (config/add_clinic.yaml, drafts)
    "insurer_confirm": S.hr_agreed,
    "hr_declined_paid": S.hr_declined,
}

# Неявный шаг между двумя соседними состояниями: из hr_wants_add в ответ СК — только через awaiting_insurer.
_IMPLIED = {(S.hr_wants_add, S.insurer_free): S.awaiting_insurer, (S.hr_wants_add, S.insurer_paid): S.awaiting_insurer}


def normalize_states(states: Sequence[str] | None) -> list[str]:
    """Шаги эталона → последовательность AddClinicState без повторов подряд, с неявными шагами.
    Неизвестное слово — ValueError (лучше упасть, чем молча потерять шаг эталона)."""
    valid = {s.value for s in S}
    out: list[str] = []
    for raw in states or []:
        if raw in valid:
            st = raw
        elif raw in ALIASES:
            st = ALIASES[raw]
            if st is None:
                continue
        else:
            raise ValueError(f"неизвестное состояние автомата в эталоне: {raw}")
        st = str(st)
        if out and (out[-1], st) in _IMPLIED:
            out.append(str(_IMPLIED[(out[-1], st)]))
        if not out or out[-1] != st:
            out.append(st)
    return out


def initial_state(answer: Mapping | None) -> str | None:
    """Начальное состояние, которое следует из ответа движка (config/add_clinic.yaml, initial):
    suggested_automation, если это состояние автомата; иначе альтернативы есть → alt_found; нет ответа → None."""
    if not answer:
        return None
    sa = answer.get("suggested_automation")
    if sa in {s.value for s in S}:
        return sa
    if answer.get("alternatives"):
        return S.alt_found.value
    return None


def check(expected_automation: Mapping | None, answer: Mapping | None, trace: Mapping | None) -> dict | None:
    """Сверка ветки автомата. Полная последовательность — если движок/автомат вернул её в trace.automation_states;
    иначе — только начальное состояние из ответа (дальше переходы идут по кнопкам менеджера, ADR 0004).
    Эталон без automation → None (не проверяется)."""
    if not expected_automation or not expected_automation.get("states"):
        return None
    exp = normalize_states(expected_automation["states"])
    got_full = (trace or {}).get("automation_states")
    if got_full is not None:
        got = normalize_states(got_full)
        return {"expected": exp, "got": got, "mode": "full", "ok": got == exp,
                "branch": expected_automation.get("branch")}
    got0 = initial_state(answer)
    return {"expected": exp, "got": [got0] if got0 else [], "mode": "initial", "ok": got0 == exp[0],
            "branch": expected_automation.get("branch")}
