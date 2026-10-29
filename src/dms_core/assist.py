"""Модель-помощник в режиме guided (этап w17-assist): понять вопрос, написанный своими словами. Вывод по-прежнему
строит код по документам (`docs/rules.md` п. 1–2) — модель ничего не решает и текста ответа не пишет.

rephrase — сообщение менеджера → один вопрос в стандартном виде («а массаж спины оплатят?» → «Входит ли массаж спины в
программу?»); один короткий вызов (qwen3:1.7b через Ollama без «размышлений» — меньше секунды на CPU Mac). Вызывается,
только когда правила маршрутизации вопрос не поняли (engine), и берётся, только если маршрут стал лучше.

Ответ модели проверяется кодом и при сомнении отбрасывается: новые слова — только из исходного вопроса, служебных слов
стандартных вопросов (config/route_markers.yaml assist_words) и равнозначных словам вопроса слов словарей
(config/care_types.yaml, service_classes.yaml procedures, route_markers.yaml assist_synonyms); числа и «названия в
кавычках» — только из исходного вопроса.

Пересказ первой фразы ответа моделью проверен и отвергнут (замер 27.10.2026): qwen3:1.7b либо копирует фразу, либо
меняет смысл («Недостаточно данных» → «Можно при условиях») — фраза ответа остаётся шаблоном кода.
Включено, когда модель задана (DMS_LLM_PROVIDER); DMS_LLM_ASSIST=0 — выключить. Промпт — config/prompts/assist_rephrase.md.

Модель недоступна (Ollama выключена, неверный адрес) — после первого сбоя помощник молчит DMS_LLM_ASSIST_PAUSE секунд
(по умолчанию 300): иначе каждый вопрос своими словами ждал бы таймаут с повторами (~15 с), хотя ответ строят правила.
"""
from __future__ import annotations

import re
import time
from functools import cache
from typing import Any

from .config import load_yaml, prompt
from .index.morph import lemmas, tokens
from .llm._common import env, log
from .llm.base import LLM, LlmError, Message, Usage

REPHRASE_MAX_CHARS = 200
ASSIST_PAUSE_S = 300.0
_down_until = 0.0   # time.monotonic(), до которого помощник не вызывается после сбоя модели
_QUOTED = re.compile(r"«([^»]+)»")
_NUM = re.compile(r"\d+(?:[.,]\d+)*")
_SCHEMA_Q: dict[str, Any] = {"type": "object", "additionalProperties": False, "required": ["question"],
                             "properties": {"question": {"type": "string"}}}


def _pause_s() -> float:
    try:
        return max(0.0, float(env("DMS_LLM_ASSIST_PAUSE", str(ASSIST_PAUSE_S))))
    except ValueError:
        return ASSIST_PAUSE_S


def reset_pause() -> None:
    """Снять паузу после сбоя (тесты; модель снова доступна)."""
    global _down_until
    _down_until = 0.0


def enabled(llm: Any) -> bool:
    """Помощник включён: модель есть и DMS_LLM_ASSIST не выключен."""
    return llm is not None and env("DMS_LLM_ASSIST", "1").strip().lower() not in ("0", "false", "no", "off")


def _groups() -> list[list[str]]:
    """Группы равнозначных слов из словарей: вид помощи с синонимами (+ каждое route_word со своим видом помощи, но не
    с другими специалистами), глаголы процедур («вырвать» — «удаление»), синонимы процедур, assist_synonyms."""
    out: list[list[str]] = []
    for spec in ((load_yaml("care_types.yaml") or {}).get("care_types") or {}).values():
        head = [str(spec.get("name") or ""), *[str(x) for x in spec.get("synonyms") or []]]
        out.append(head)
        out += [[str(w), *head] for w in spec.get("route_words") or []]
    proc = (load_yaml("service_classes.yaml") or {}).get("procedures") or {}
    out += [[str(k), str(v)] for k, v in (proc.get("verbs") or {}).items()]
    out += [[str(k), *[str(x) for x in v or []]] for k, v in (proc.get("synonyms") or {}).items()]
    out += [[str(x) for x in g] for g in (load_yaml("route_markers.yaml") or {}).get("assist_synonyms") or []]
    return out


@cache
def _lemma_groups() -> tuple[tuple[frozenset[str], frozenset[str]], ...]:
    """(леммы-признаки группы без общих слов, все леммы группы)."""
    common = _assist_words() | GENERIC
    res = []
    for g in _groups():
        # леммы и сами слова: отдельное слово словаря pymorphy разбирает иначе, чем в вопросе («зубник» →
        # «зубникнуть», а «к зубнику» → «зубник»)
        lem = frozenset(w for phrase in g for w in (*lemmas(phrase), *tokens(phrase)) if len(w) > 1)
        if lem:
            res.append((frozenset(lem - common), lem))
    return tuple(res)


@cache
def _assist_words() -> frozenset[str]:
    return frozenset(lemmas(" ".join((load_yaml("route_markers.yaml") or {}).get("assist_words") or [])))


GENERIC = frozenset({"помощь", "лечение", "приём", "прием", "врач", "услуга", "медицинский", "обслуживание"})


def allowed_words(source: str) -> set[str]:
    """Леммы, которые модель может использовать: слова исходного текста, служебные слова стандартных вопросов и
    равнозначные им слова словарей (только группы, слово которых есть в тексте: «вырвать» → «удаление», но «лечить
    зубы» не даёт «удаление» — ревизия w17: подмена услуги давала ложное «нельзя»)."""
    have = set(lemmas(source))
    out = have | _assist_words()
    for keys, group in _lemma_groups():
        if keys & have:
            out |= group
    return out


def _new_facts(text: str, source: str) -> list[str]:
    """Что в text не опирается на source: леммы вне allowed_words, числа и «названия» вне source."""
    have = allowed_words(source)
    bad = [w for w in lemmas(text) if w not in have and not w.isdigit()]
    src_low = source.casefold()
    bad += [n for n in _NUM.findall(text) if n not in source]
    bad += [q for q in _QUOTED.findall(text) if q.casefold() not in src_low]
    return bad


def _call(llm: LLM, name: str, user: str, schema: dict[str, Any], key: str, max_tokens: int,
          usage: Usage | None) -> str | None:
    from .agent import extract_json
    from .llm._common import structured_override

    global _down_until
    if time.monotonic() < _down_until:
        return None   # недавно был сбой — вопрос понимают правила, без ожидания таймаута
    try:
        with structured_override("on"):
            turn = llm.chat([Message("system", prompt(name)), Message("user", user)], None, json_schema=schema,
                            max_tokens=max_tokens)
    except LlmError as e:
        _down_until = time.monotonic() + _pause_s()
        log.info("assist %s: модель недоступна (%s), пауза %.0f с", name, type(e).__name__, _pause_s())
        return None
    if usage is not None:
        usage.input_tokens += turn.usage.input_tokens
        usage.output_tokens += turn.usage.output_tokens
    try:
        data = extract_json(turn.text)
    except (ValueError, TypeError):
        return None
    value = data.get(key) if isinstance(data, dict) else None
    return value.strip() if isinstance(value, str) and value.strip() else None


def rephrase(question: str, llm: LLM, usage: Usage | None = None) -> str | None:
    """Вопрос своими словами → стандартный вопрос; None — модель не помогла или добавила своё (проверка выше)."""
    got = _call(llm, "assist_rephrase", question, _SCHEMA_Q, "question", 120, usage)
    if not got or len(got) > REPHRASE_MAX_CHARS or got.casefold() == question.strip().casefold():
        return None
    bad = _new_facts(got, question)
    if bad:
        log.info("assist rephrase: отброшено, новых слов %d", len(bad))   # без текста вопроса (`docs/rules.md` п. 4)
        return None
    return got
