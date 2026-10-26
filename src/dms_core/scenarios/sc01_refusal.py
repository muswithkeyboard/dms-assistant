"""SC 01 — метка разбора отказа клиники (ТЗ п. 5.1, план §6.6). Правила — только config/verdict_rules.yaml.

Правила применяются сверху вниз, первое сработавшее задаёт метку. Условие `when` — словарь «проверка → значение»:
- exclusion / care_in_program / clinic_in_program — точное совпадение с yes / absent / not_found;
- documents — consistent / contradictory;
- conditions — `present` (список условий не пуст) или `none` (пуст);
- пустой `when` срабатывает всегда (последнее правило — «недостаточно данных»).

`not_found` («не нашли») не совпадает ни с yes, ни с absent, поэтому ни одно правило «нельзя» (оно требует
exclusion=yes или absent) на нём не срабатывает (`docs/rules.md` п. 3а). Значение None (проверка не выполнена) тоже
ничему не равно. Уточнение метки: `note` правила, а для «нельзя» при найденной альтернативе —
notes.denied_with_alternative.
"""
from __future__ import annotations

from typing import Any

from ..config import load_yaml
from ..constants import Label
from ..schemas import Checks

_SCALAR = ("exclusion", "care_in_program", "clinic_in_program", "documents")


def _rules() -> dict[str, Any]:
    return load_yaml("verdict_rules.yaml") or {}


def _matches(key: str, want: Any, checks: Checks, facts: dict[str, Any] | None = None) -> bool:
    """Одно условие правила против проверок. Неизвестный ключ — ошибка конфига, а не молчаливое «не совпало».
    refusal: yes/no — в обращении есть отказ клиники (слот маршрутизации), передаётся движком в facts;
    clinic_confirmed: yes/no — клиника подтверждена выбором менеджера (ADR 0010), тоже из facts;
    clinic_named: yes/no — клиника названа в вопросе. Значение-список — «любое из»."""
    if key in ("refusal", "clinic_confirmed", "clinic_named"):
        # clinic_confirmed: менеджер выбрал строку перечня или «нет в перечне», и guided её учёл (ADR 0010);
        # clinic_named: в вопросе названа клиника (слот маршрутизации)
        has = bool((facts or {}).get(key))
        return has if want in (True, "yes") else not has
    if key == "conditions":
        if want == "present":
            return bool(checks.conditions)
        if want == "none":
            return not checks.conditions
        raise ValueError(f"verdict_rules.yaml: conditions — только present/none, а не {want!r}")
    if key not in _SCALAR:
        raise ValueError(f"verdict_rules.yaml: неизвестная проверка {key!r}")
    # yaml читает `yes` как True: сравниваем с исходным словом конфига
    if want is None or want == "null":           # проверка не выполнялась (например, клиника не названа)
        return getattr(checks, key) is None
    if isinstance(want, list):                   # «любое из»: [absent, not_found]
        return getattr(checks, key) in {"yes" if w is True else "no" if w is False else str(w) for w in want}
    want_s = "yes" if want is True else "no" if want is False else str(want)
    return getattr(checks, key) == want_s


def decide_label(checks: Checks | None, has_alternative: bool,
                 facts: dict[str, Any] | None = None) -> tuple[Label, str | None, str]:
    """(метка, уточнение, id правила) по первому сработавшему правилу config/verdict_rules.yaml."""
    cfg = _rules()
    notes = cfg.get("notes", {}) or {}
    ch = checks or Checks()
    for rule in cfg.get("rules", []):
        when = rule.get("when") or {}
        if all(_matches(k, v, ch, facts) for k, v in when.items()):
            label = Label(rule["label"])
            note = rule.get("note")
            if label is Label.denied and has_alternative:
                note = notes.get("denied_with_alternative", note)
            return label, note, str(rule["id"])
    # конфиг без правила по умолчанию — честное «недостаточно данных»
    return Label.insufficient, None, "no_rule"
