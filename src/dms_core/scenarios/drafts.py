"""Черновики писем автомата добавления клиники (ADR 0004, план §6.9, CONTRACTS §8).

Письмо — целиком шаблон из config/texts_ru/drafts.yaml; код подставляет только значения полей {имя}.
Правила:
- неизвестное (пустое) поле → «[ЗАПОЛНИТЬ]» и имя поля в `unknown_fields`;
- значение поля, в котором фильтр config/forbidden_promises.yaml находит обещание (сроки, «гарантируем»,
  «согласовано»…), в письмо не попадает — вместо него «[ЗАПОЛНИТЬ]» (менеджер впишет сам);
- итоговый текст (тема + тело) ещё раз проходит promises.find_promises с template_id (для allow_in): найдено →
  ValueError, шаблон испорчен (тест на всех шаблонах — tests/scenarios/test_drafts.py).
Письма не отправляются никогда: Draft.auto_send = False.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import re
from typing import Any

from ..config import load_yaml
from ..constants import AddClinicState, DraftTo
from ..promises import find_promises
from ..schemas import Alternative, Answer, Draft, Request

FILL = "[ЗАПОЛНИТЬ]"
_FIELD_RE = re.compile(r"\{(\w+)\}")
_FRANCHISE_NA = "—"


def templates() -> dict[str, dict[str, Any]]:
    return (load_yaml("texts_ru/drafts.yaml") or {}).get("templates", {}) or {}


def template_fields(template_id: str) -> list[str]:
    """Имена полей шаблона в порядке появления (тема, затем тело), без повторов."""
    tpl = _template(template_id)
    names = _FIELD_RE.findall(str(tpl.get("subject", "")) + "\n" + str(tpl.get("body", "")))
    return list(dict.fromkeys(names))


def _template(template_id: str) -> dict[str, Any]:
    tpl = templates().get(template_id)
    if not tpl:
        raise KeyError(f"нет шаблона черновика «{template_id}» в config/texts_ru/drafts.yaml")
    return tpl


def draft_id(request_id: str, clinic_key: str, stage: AddClinicState | str, template_id: str) -> str:
    """Идентификатор черновика из ключа идемпотентности (request_id + clinic + этап + шаблон): повтор шага даёт
    тот же id, и автомат не добавляет второй черновик (ADR 0004)."""
    raw = "|".join([request_id, clinic_key, str(stage), template_id])
    return "d_" + hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def _clean(value: str | None) -> str:
    return (value or "").strip()


def promise_fields(template_id: str, fields: dict[str, str]) -> dict[str, list[str]]:
    """Поля шаблона, значение которых содержит обещание (config/forbidden_promises.yaml): имя → найденные фразы.
    Такие поля render заменяет на «[ЗАПОЛНИТЬ]»; перечень — для пометки менеджеру, почему поле пустое."""
    out: dict[str, list[str]] = {}
    for name in template_fields(template_id):
        value = _clean(fields.get(name))
        hits = find_promises(value, template_id) if value else []
        if hits:
            out[name] = hits
    return out


def render(template_id: str, fields: dict[str, str], *, stage: AddClinicState,
           draft_id: str | None = None, now: dt.datetime | None = None) -> Draft:
    """Черновик по шаблону. Подстановка — один проход регулярным выражением: фигурные скобки внутри значений
    повторно не раскрываются."""
    tpl = _template(template_id)
    used: dict[str, str] = {}
    unknown: list[str] = []
    for name in template_fields(template_id):
        value = _clean(fields.get(name))
        # Значение с обещанием (например, вставленный текст «гарантируем в течение 3 дней») не подставляем.
        if not value or find_promises(value, template_id):
            unknown.append(name)
            used[name] = FILL
        else:
            used[name] = value

    def fill(text: str) -> str:
        return _FIELD_RE.sub(lambda m: used.get(m.group(1), FILL), text)

    subject = " ".join(fill(str(tpl.get("subject", ""))).split())
    body = fill(str(tpl.get("body", ""))).strip() + "\n"
    hits = find_promises(subject + "\n" + body, template_id)
    if hits:
        raise ValueError(f"шаблон «{template_id}» содержит запрещённое обещание: {hits}")
    return Draft(
        id=draft_id or f"d_{template_id}",
        to=DraftTo(str(tpl.get("to", "hr"))),
        stage=stage,
        subject=subject,
        body=body,
        fields={k: v for k, v in used.items() if v != FILL},
        unknown_fields=unknown,
        created_at=now or dt.datetime.now(dt.UTC),
    )


def letters() -> dict[str, dict[str, Any]]:
    """Шаблоны писем из ответа (config/texts_ru/drafts.yaml letters) — отдельно от шаблонов автомата."""
    return dict((load_yaml("texts_ru/drafts.yaml") or {}).get("letters") or {})


def letter_fields(template_id: str) -> list[str]:
    tpl = letters()[template_id]
    return list(dict.fromkeys(_FIELD_RE.findall(str(tpl.get("subject", "")) + "\n" + str(tpl.get("body", "")))))


def render_letter(template_id: str, fields: dict[str, str]) -> dict[str, Any]:
    """Письмо из ответа (шаблоны answer_hr / answer_insurer): не черновик автомата (Draft привязан к его шагам), а
    текст для окна «Письмо» — тема, тело, незаполненные поля. Подстановка — как у render: значение с обещанием
    (config/forbidden_promises.yaml) не подставляется; поля из «optional» могут быть пустыми — их строка убирается."""
    tpl = letters()[template_id]
    optional = set(tpl.get("optional") or [])
    used: dict[str, str] = {}
    unknown: list[str] = []
    for name in letter_fields(template_id):
        value = _clean(fields.get(name))
        if value and not find_promises(value, template_id):
            used[name] = value
        elif not value and name in optional:
            used[name] = ""
        else:
            unknown.append(name)
            used[name] = FILL

    def fill(text: str) -> str:
        return _FIELD_RE.sub(lambda m: used.get(m.group(1), FILL), text)

    subject = " ".join(fill(str(tpl.get("subject", ""))).split())
    body = re.sub(r"\n{3,}", "\n\n", fill(str(tpl.get("body", ""))).strip()) + "\n"
    hits = find_promises(subject + "\n" + body, template_id)
    if hits:
        raise ValueError(f"шаблон «{template_id}» содержит запрещённое обещание: {hits}")
    return {"template": template_id, "to": str(tpl.get("to", "hr")), "subject": subject, "body": body,
            "unknown_fields": unknown}


def format_alternatives(alts: list[Alternative]) -> str:
    """Список клиник для письма HR: название, адрес и франшиза — только поля строки перечня (без источников)."""
    lines = []
    for a in alts:
        r = a.clinic
        parts = [r.clinic_name]
        if r.address:
            parts.append(r.address)
        if r.franchise_pct == _FRANCHISE_NA:
            parts.append("франшиза не применяется")
        elif isinstance(r.franchise_pct, int | float):
            parts.append(f"франшиза {r.franchise_pct:g} %")
        if r.by_referral:
            parts.append("по направлению")
        lines.append("— " + ", ".join(parts))
    return "\n".join(lines)


def draft_fields(answer: Answer | None, request: Request, store: Any = None) -> dict[str, str]:
    """Поля черновика из контекста ответа, слотов запроса и карточки договора (юрлицо, номер договора).
    Пустых значений нет: отсутствующее поле в словарь не попадает и станет «[ЗАПОЛНИТЬ]»."""
    out: dict[str, str] = {}
    ctx = answer.context if answer is not None else None
    cid = (ctx.contract_id if ctx else None) or request.slots.get("contract_id") or request.slots.get("contract")
    program = (ctx.program if ctx else None) or request.slots.get("program")
    if cid and store is not None:
        try:
            c = store.contract(cid)
        except (KeyError, ValueError, LookupError):
            c = None
        if c is not None:
            out["legal_entity"] = c.legal_entity
            if c.number:
                out["contract"] = f"№ {c.number}"
            if not program and len(c.programs) == 1:
                program = c.programs[0].name           # единственная программа договора — факт карточки
    if program:
        out["program"] = f"«{program}»"
    for key in ("clinic", "care_type", "address"):
        v = _clean(request.slots.get(key))
        if v:
            out[key] = v
    if answer is not None and answer.alternatives:
        out["alternatives"] = format_alternatives(answer.alternatives)
    return out
