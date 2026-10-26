"""Агент одного договора: модель ↔ инструменты → DraftAnswer (docs/CONTRACTS.md §7, план §6.4, ADR 0005).

Цикл (≤ max_steps вызовов модели):
1. Системный промпт = config/prompts/agent_system.md + промпт сценария (agent_sc01 / agent_sc02) + JSON Schema
   DraftAnswer. Сообщение пользователя — контекст чата, факты маршрутизатора и вопрос в разделителях.
2. Модель вызывает инструменты из `bind_tools(contract_id)`: договор привязан кодом, параметра contract_id у модели
   нет. Результаты уходят обратно как сообщения role=tool (данные, не инструкции).
3. Последний разрешённый шаг — без инструментов и с json_schema: модель обязана вернуть итог.
4. Итог разбирается в DraftAnswer; невалидный JSON → одна попытка исправления с текстом ошибки, затем `error`.
5. Правила, которые проверяет код, а не просьба к модели (`_enforce`): contract_id = привязанный договор;
   `absent` без просмотра документа нужного типа → `not_found`; альтернатива — только строка клиник из этого
   прогона; утверждение с числом, которого нет в значащих полях результатов инструментов (не ref/id), вопросе
   и контексте, выбрасывается, summary с таким числом очищается (арифметика дат и сумм — только payments_next /
   date_calc; numtext.py). ToolRecord хранит краткий итог вызова и результат вычисления — по ним
   citations.verify подтверждает absent и утверждения о взносах (ревизия w2, дефекты 4, 5).
Тексты документов и вопросов в журнал не пишутся (`docs/rules.md` п. 4).
"""
from __future__ import annotations

import datetime as dt
import json
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from pydantic import ValidationError

from .config import prompt, prompt_version
from .constants import Scenario
from .llm.base import LLM, LlmError, Message, ToolCall, Usage
from .numtext import known_numbers, numbers
from .schemas import ChatContext, DraftAnswer
from .store.db import KitStore
from .tools._common import resolve
from .tools.registry import bind_tools, collect_refs

MAX_CALLS_PER_STEP = 6      # вызовов инструментов за один ход модели; лишние получают ошибку
STEP_MAX_TOKENS = 4096

# Какой инструмент «просматривает документ целиком» для проверки absent (`docs/rules.md` п. 3а, план §6.5):
# вид помощи — матрица программы; клиника — перечень клиник; исключения — поиск/чтение пунктов.
ABSENT_REQUIRES = {
    "care_in_program": {"care_matrix", "compare_programs"},
    "clinic_in_program": {"clinics_query", "compare_programs"},
    "exclusion": {"search_clauses", "get_clause"},
}

# Словарь условий SC 01 (schemas.Checks.conditions, config/verdict_rules.yaml: conditions present/none).
CONDITIONS = ("franchise", "referral", "approval", "zone", "limit", "other")


@dataclass
class ToolRecord:
    name: str
    args: dict[str, Any]
    refs: set[str]
    error: str | None = None
    # Краткий итог для citations.verify (дефект 4 ревизии w2): care_matrix — строки (ref, программа, вид помощи,
    # present), clinics_query — total, search_clauses/get_clause — hits. Без текстов документов.
    summary: dict[str, Any] = field(default_factory=dict)
    # Результат вычисления payments_next / date_calc целиком (дефект 5): утверждение с этим числом/датой
    # подтверждается самим результатом инструмента.
    result: dict[str, Any] | None = None


class RunRefs(set[str]):
    """Набор ref прогона + записи инструментов (`tools`): verify(run.draft, run.refs, …) видит и то, и другое."""

    def __init__(self, refs: Iterable[str] = (), tools: list[ToolRecord] | None = None) -> None:
        super().__init__(refs)
        self.tools: list[ToolRecord] = list(tools or [])


@dataclass
class AgentRun:
    draft: DraftAnswer
    tools: list[ToolRecord]
    steps: int
    usage: Usage
    model: str
    error: str | None
    warnings: list[str] = field(default_factory=list)   # что поправил код (без текстов документов)
    prompt_version: str = ""

    @property
    def refs(self) -> RunRefs:
        """Все ref, которые вернули инструменты в этом прогоне (вход citations.verify), вместе с записями."""
        out: set[str] = set()
        for t in self.tools:
            out |= t.refs
        return RunRefs(out, self.tools)


CALC_TOOLS = ("payments_next", "date_calc")


def summarize(name: str, result: dict[str, Any]) -> dict[str, Any]:
    """Краткий итог вызова для проверок кодом (без текстов документов)."""
    if name == "care_matrix":
        return {"rows": [{"ref": r.get("ref"), "program": r.get("program"), "care_type": r.get("care_type"),
                          "present": r.get("present")} for r in result.get("rows") or []]}
    if name == "clinics_query":
        return {"total": result.get("total", 0), "rows": len(result.get("rows") or [])}
    if name == "search_clauses":
        return {"hits": len(result.get("hits") or [])}
    if name == "get_clause":
        return {"hits": len(result.get("clauses") or [])}
    return {}


def scenario_prompt(scenario: Scenario | str) -> str:
    return "agent_sc01" if str(scenario) == Scenario.sc01.value else "agent_sc02"


def system_prompt(scenario: Scenario | str) -> str:
    """Стабильная часть (кэш провайдера): общие правила + сценарий + схема ответа. Без дат и id."""
    schema = json.dumps(DraftAnswer.model_json_schema(), ensure_ascii=False, sort_keys=True)
    return (f"{prompt('agent_system')}\n\n{prompt(scenario_prompt(scenario))}\n\n"
            f"## JSON Schema итогового ответа (DraftAnswer)\n{schema}\n")


def user_message(cid: str, legal_entity: str, question: str, scenario: Scenario | str, slots: dict[str, Any],
                 context: ChatContext) -> str:
    facts = "\n".join(f"- {k}: {v}" for k, v in sorted(slots.items()) if v not in (None, "")) or "- (нет)"
    date = context.date.isoformat() if context.date else f"не указана (сегодня {dt.date.today().isoformat()})"
    return (
        "<context>\n"
        f"Договор: {cid} ({legal_entity}). Инструменты уже привязаны к этому договору.\n"
        f"Программа застрахованного: {context.program or slots.get('program') or 'не указана'}\n"
        f"Дата расчёта: {date}\n"
        f"Сценарий: {scenario}\n"
        f"Факты из обращения (маршрутизатор):\n{facts}\n"
        "</context>\n"
        "<question>\n"
        f"{question}\n"
        "</question>"
    )


_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL)
_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


def extract_json(text: str) -> Any:
    """JSON из ответа модели: без <think>, без ```-ограждений, от первой «{» до последней «}»."""
    s = _THINK_RE.sub("", text or "").strip()
    m = _FENCE_RE.search(s)
    if m:
        s = m.group(1).strip()
    start, end = s.find("{"), s.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("в ответе нет JSON-объекта")
    return json.loads(s[start:end + 1])


def parse_draft(text: str) -> tuple[DraftAnswer | None, str | None]:
    """(черновик, None) или (None, причина по-русски). Причина — места и типы ошибок, без значений полей."""
    try:
        data = extract_json(text)
    except (ValueError, json.JSONDecodeError) as exc:
        return None, f"не JSON: {exc}"
    try:
        return DraftAnswer.model_validate(data), None
    except ValidationError as exc:
        errs = "; ".join(f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors()[:5])
        return None, f"не по схеме DraftAnswer: {errs}"


# ---------------------------------------------------------------- проверки кодом (не просьбой к модели)

def _known_numbers(results: list[Any], extra: list[str], default_year: int | None = None) -> set[str]:
    """Числа значащих полей результатов инструментов (не ref/id — дефект 7), вопроса и контекста."""
    return known_numbers([*results, *extra], default_year=default_year)


def _enforce(draft: DraftAnswer, cid: str, records: list[ToolRecord], results: list[Any],
             extra_texts: list[str], default_year: int | None = None) -> tuple[DraftAnswer, list[str]]:
    warnings: list[str] = []
    data = draft.model_dump()
    # 1. Договор ответа — только привязанный (ADR 0005): модель не может «переключить» договор.
    if data["contract_id"] != cid:
        warnings.append("contract_id ответа заменён на договор чата")
        data["contract_id"] = cid
    # 2. absent только после просмотра документа нужного типа (`docs/rules.md` п. 3а): инструмент вызван без ошибки
    #    и в looked_at что-то перечислено; иначе — not_found («не нашли» не равно «нет в документах»).
    #    Инструмент должен что-то вернуть: строки матрицы / перечня, найденные пункты. Полная проверка (та ли
    #    программа, есть ли строка present=false, нет ли клиники в перечне) — citations.verify.
    ok_tools = {r.name for r in records if r.error is None and _returned_something(r)}
    checks = data.get("checks")
    if checks:
        for key, need in ABSENT_REQUIRES.items():
            if checks.get(key) == "absent" and (not data["looked_at"] or not (need & ok_tools)):
                checks[key] = "not_found"
                warnings.append(f"checks.{key}: absent → not_found (документ нужного типа не просмотрен)")
        # Условия — только из словаря Checks.conditions; незнакомое значение модели → other (осторожно: «при
        # условиях», а не «можно»).
        conds = []
        for c in checks.get("conditions") or []:
            c = c if c in CONDITIONS else "other"
            if c not in conds:
                conds.append(c)
        if conds != list(checks.get("conditions") or []):
            warnings.append("checks.conditions: незнакомые значения заменены на other")
        checks["conditions"] = conds
    # 3. Альтернатива — только строка перечня клиник, которую вернул clinics_query/compare_programs в этом прогоне.
    clinic_refs: set[str] = set()
    for r in records:
        if r.name in ("clinics_query", "compare_programs"):
            clinic_refs |= r.refs
    kept_alt = [a for a in data["alternatives"] if a["row_id"] in clinic_refs]
    if len(kept_alt) != len(data["alternatives"]):
        warnings.append(f"альтернатив отброшено: {len(data['alternatives']) - len(kept_alt)} (не из clinics_query)")
    data["alternatives"] = kept_alt
    # 4. Числа (даты, суммы, проценты, лимиты) — только из инструментов, вопроса и контекста: модель не считает.
    #    Однозначные числа тоже проверяются; номера пунктов в самом утверждении («п. 2.6.1») — нет (numtext).
    known = _known_numbers(results, extra_texts, default_year)
    kept = []
    for st in data["statements"]:
        unknown = numbers(st["text"], default_year=default_year) - known
        if unknown:
            warnings.append(f"утверждение отброшено: число не из инструментов ({len(unknown)} шт.)")
            continue
        kept.append(st)
    data["statements"] = kept
    # 5. summary с числом не из инструментов не показывается: compose соберёт вывод из живых утверждений
    #    (дефект 3 ревизии w2).
    if numbers(data["summary"], default_year=default_year) - known:
        warnings.append("summary удалён: в нём число не из инструментов")
        data["summary"] = ""
    return DraftAnswer.model_validate(data), warnings


def _returned_something(r: ToolRecord) -> bool:
    if r.name == "care_matrix":
        return bool(r.summary.get("rows"))
    if r.name == "clinics_query":
        return bool(r.summary.get("rows"))
    if r.name in ("search_clauses", "get_clause"):
        return bool(r.summary.get("hits"))
    return True


# ---------------------------------------------------------------- цикл

def _add(u: Usage, v: Usage) -> None:
    u.input_tokens += v.input_tokens
    u.output_tokens += v.output_tokens


def run_agent(llm: LLM, contract_id: str, question: str, *, scenario: Scenario | str, slots: dict[str, Any],
              context: ChatContext, store: KitStore | None = None, max_steps: int = 8) -> AgentRun:
    """Один договор — один прогон. Ошибки модели и JSON не бросаются: AgentRun.error по-русски."""
    cid, st = resolve(contract_id, store)
    specs, execute = bind_tools(cid, st)
    schema = DraftAnswer.model_json_schema()
    sp_name = scenario_prompt(scenario)
    version = f"agent_system@{prompt_version('agent_system')}+{sp_name}@{prompt_version(sp_name)}"
    messages = [Message("system", system_prompt(scenario)),
                Message("user", user_message(cid, st.contract(cid).legal_entity, question, scenario, slots,
                                             context))]
    records: list[ToolRecord] = []
    results: list[Any] = []
    usage = Usage()
    steps = 0
    warnings: list[str] = []
    model = getattr(llm, "model", "")

    def fail(msg: str) -> AgentRun:
        return AgentRun(draft=DraftAnswer(contract_id=cid, summary=""), tools=records, steps=steps, usage=usage,
                        model=model, error=msg, warnings=warnings, prompt_version=version)

    final_text: str | None = None
    try:
        while steps < max_steps:
            last = steps == max_steps - 1
            # Последний шаг: инструменты не передаются, итог — строго по схеме.
            turn = llm.chat(messages, None if last else specs, json_schema=schema if last else None,
                            max_tokens=STEP_MAX_TOKENS)
            steps += 1
            _add(usage, turn.usage)
            model = turn.model or model
            if turn.tool_calls and not last:
                messages.append(Message("assistant", turn.text, tool_calls=list(turn.tool_calls)))
                for i, call in enumerate(turn.tool_calls):
                    result = _run_tool(execute, call, i)
                    err = result.get("error") if isinstance(result, dict) else None
                    refs = set() if err else collect_refs(result)
                    records.append(ToolRecord(
                        name=call.name, args=dict(call.args or {}), refs=refs, error=err,
                        summary={} if err else summarize(call.name, result),
                        result=dict(result) if call.name in CALC_TOOLS and not err else None))
                    if not err:
                        results.append(result)
                    messages.append(Message("tool", json.dumps(result, ensure_ascii=False, default=str),
                                            tool_call_id=call.id, name=call.name))
                if steps == max_steps - 1:
                    messages.append(Message("user", "Лимит шагов исчерпан: инструменты больше недоступны. "
                                                    "Верни итоговый ответ — только JSON по схеме DraftAnswer."))
                continue
            if turn.tool_calls:
                warnings.append("вызов инструмента после лимита шагов не выполнен")
            final_text = turn.text
            break
        if final_text is None:
            return fail("модель не вернула итоговый ответ")

        draft, problem = parse_draft(final_text)
        if draft is None:
            # Одна попытка исправления: модель видит причину (места ошибок, без значений) и схему.
            warnings.append(f"итог не прошёл проверку: {problem}")
            messages.append(Message("assistant", final_text))
            messages.append(Message("user", f"Итоговый ответ не принят ({problem}). Верни только JSON-объект "
                                            "по схеме DraftAnswer, без пояснений и без ```."))
            turn = llm.chat(messages, None, json_schema=schema, max_tokens=STEP_MAX_TOKENS)
            steps += 1
            _add(usage, turn.usage)
            draft, problem = parse_draft(turn.text)
            if draft is None:
                return fail(f"невалидный итоговый JSON после исправления: {problem}")
    except LlmError as exc:
        return fail(f"ошибка модели: {exc}")

    extra = [question, *(str(v) for v in slots.values()), context.date.isoformat() if context.date else ""]
    year = (context.date or dt.date.today()).year
    draft, fixed = _enforce(draft, cid, records, results, extra, year)
    warnings.extend(fixed)
    return AgentRun(draft=draft, tools=records, steps=steps, usage=usage, model=model, error=None,
                    warnings=warnings, prompt_version=version)


def _run_tool(execute: Any, call: ToolCall, index: int) -> dict[str, Any]:
    if index >= MAX_CALLS_PER_STEP:
        return {"error": f"не больше {MAX_CALLS_PER_STEP} вызовов инструментов за один шаг"}
    args = call.args if isinstance(call.args, dict) else {}
    return execute(call.name, args)
