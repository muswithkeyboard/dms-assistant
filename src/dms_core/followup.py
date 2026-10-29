"""Ответ менеджера на уточняющий вопрос ассистента (правило 2: нет факта → вопрос) продолжает исходный вопрос.

Ассистент спросил «Какая программа у застрахованного?», менеджер ответил «Стандарт» — это не новый вопрос, а
недостающий факт к прошлому. Движку передаётся исходный вопрос вместе с ответом: «Клиника не дала сделать МРТ
колена — правомерно? Стандарт», и маршрутизация находит программу, клинику, договор как в одном вопросе
(ревизия интерфейса w16: ответ на уточнение терял исходный вопрос).

Правило склейки — только по форме, без догадок о смысле: прошлый ответ ассистента — уточнение
(confidence need_clarification), а новое сообщение короткое и само не вопрос.
"""
from __future__ import annotations

import re

from .config import load_yaml
from .constants import Confidence
from .schemas import Message

FOLLOWUP_MAX_WORDS = 8      # длиннее — это уже свой вопрос, а не ответ на уточнение
_WORD = re.compile(r"[\wА-Яа-яЁё-]+")


def is_short_reply(text: str) -> bool:
    """Короткий ответ, а не новый вопрос: ≤ FOLLOWUP_MAX_WORDS слов и без вопросительного знака."""
    t = (text or "").strip()
    return bool(t) and "?" not in t and len(_WORD.findall(t)) <= FOLLOWUP_MAX_WORDS


def followup_question(messages: list[Message], text: str) -> str | None:
    """Исходный вопрос + ответ менеджера, если последнее сообщение ассистента — уточняющий вопрос, а text —
    короткий ответ на него; иначе None (text — самостоятельный вопрос). messages — сообщения обращения до text."""
    if not is_short_reply(text):
        return None
    last = next((m for m in reversed(messages) if m.role == "assistant"), None)
    if last is None or last.answer is None or last.answer.confidence is not Confidence.need_clarification:
        return None
    # вопрос, на который ассистент ответил уточнением: последнее сообщение менеджера перед этим ответом;
    # цепочка уточнений («программа?» → «клиника?») уже склеена в нём на прошлом шаге — берётся как есть
    idx = messages.index(last)
    prev = next((m for m in reversed(messages[:idx]) if m.role == "user" and m.text), None)
    if prev is None:
        return None
    base = (prev.asked or prev.text or "").strip()
    # ответ на вопрос о программе или клинике — словами, по которым маршрутизация узнаёт слот
    # («по программе «Стандарт»», «в клинике Медси»): reply.yaml followup_glue, по первому совпавшему вопросу
    asked_q = " ".join(last.answer.questions).casefold()
    for pat, tpl in (load_yaml("texts_ru/reply.yaml") or {}).get("followup_glue") or []:
        if re.search(pat, asked_q):
            # прошлый ответ на тот же вопрос не подошёл («По программе «омс».») — заменяется новым, а не копится
            prev_glue = glued_reply(base)
            if prev_glue is not None and prev_glue[0] == pat:
                head = tpl.partition("{text}")[0]
                base = base[:base.rfind(head)].rstrip()
            return f"{base} {tpl.format(text=text.strip())}"
    return f"{base} {text.strip()}"


def glued_reply(question: str) -> tuple[str, str] | None:
    """Склеенный followup_question ответ менеджера в конце вопроса → (шаблон вопроса ассистента, ответ):
    «… По программе «омс».» → («программ», «омс»). Нет склейки — None. Нужно, чтобы ответ, которого нет среди
    вариантов, не превращался в другой слот (ревизия: «омс» стал клиникой) и вопрос не повторялся молча."""
    for pat, tpl in (load_yaml("texts_ru/reply.yaml") or {}).get("followup_glue") or []:
        head, _, tail = tpl.partition("{text}")
        i = (question or "").rfind(head)
        m = re.match(re.escape(head) + r"(?P<text>.+?)" + re.escape(tail) + r"\s*$", question[i:]) if i >= 0 else None
        if m:
            return pat, m.group("text").strip()
    return None


CLARIFY_MAX_CHARS = 300


def clarify_reply(messages: list[Message], clarify: str, text: str) -> str | None:
    """Ответ менеджера на конкретный вопрос из «Что уточнить» (кнопка ответа или «свой ответ» в блоке уточнения):
    исходный вопрос + этот вопрос с ответом — всегда, а не только после ответа-уточнения (followup_question):
    вопросы «Что уточнить» бывают и у обычного ответа, и «Да» само по себе потеряло бы смысл. Вопрос о программе
    или клинике — словами из reply.yaml followup_glue (маршрутизация узнаёт слот); иначе «<вопрос без ?> — <ответ>.».
    Нет исходного вопроса — None (ответ уходит как обычный вопрос)."""
    last = next((m for m in reversed(messages) if m.role == "assistant" and m.answer is not None), None)
    if last is None:
        return None
    idx = messages.index(last)
    prev = next((m for m in reversed(messages[:idx]) if m.role == "user" and m.text), None)
    if prev is None:
        return None
    base, q, ans = (prev.asked or prev.text or "").strip(), clarify.strip(), text.strip()
    return f"{base} {clarify_fact(q, ans)}"


def clarify_fact(question: str, answer: str) -> str:
    """Ответ на вопрос «Что уточнить» → факт словами (reply.yaml clarify_glue / clarify_yesno): «В клинике Медси.»,
    «направление врача есть.»; иначе «<вопрос без ?> — <ответ>.»."""
    cfg = load_yaml("texts_ru/reply.yaml") or {}
    q = question.strip().rstrip("?？ ").strip()
    low, ans = q.casefold(), answer.strip()
    for pat, tpl in cfg.get("clarify_glue") or []:
        if re.search(pat, low):
            return tpl.format(text=ans)
    a = ans.casefold().rstrip(".! ")
    # выбран один из вариантов вопроса «А или Б?» — сам вариант и есть факт («Плановая госпитализация.»)
    if " или " in low and any(a == alt.strip() or alt.strip().startswith(a) for alt in low.split(" или ")):
        return ans.rstrip(".") + "."
    yes, no = a in (cfg.get("clarify_yes") or []), a in (cfg.get("clarify_no") or [])
    if yes or no:
        for pat, yes_tpl, no_tpl in cfg.get("clarify_yesno") or []:
            m = re.match(pat, low)
            if m:
                return (yes_tpl if yes else no_tpl).format(x=q[m.end():].strip()) + "."
    return f"{q} — {ans}."
