"""Морфология для поиска (план §4): токены → леммы pymorphy3 с кэшем.

Нормализация: регистр, «ё» → «е» (в документах СК пишут и «налёт», и «налет»), латиница и цифры остаются как есть
(«AIR FLOW», «МРТ», «2.6.1»). Слова короче 2 знаков и служебные слова отбрасываются.
"""
from __future__ import annotations

import re
from functools import cache, lru_cache

_TOKEN_RE = re.compile(r"[0-9a-zа-яё]+(?:[.\-][0-9a-zа-яё]+)*", re.IGNORECASE)

# Служебные слова, не несущие смысла для поиска по условиям (предлоги, союзы, частицы).
STOPWORDS = frozenset(
    ["и", "в", "во", "на", "по", "с", "со", "к", "ко", "о", "об", "от", "до", "из", "за", "для", "при", "без", "над", "под", "про", "не", "ни", "а", "но", "или", "либо", "же", "ли", "бы", "что", "как", "это", "то", "так", "его", "ее", "их", "он", "она", "они", "мы", "вы", "я", "который", "также", "т", "д", "п", "пп", "ст"]
)


@cache
def _analyzer():  # type: ignore[no-untyped-def]  # pymorphy3 без аннотаций
    import pymorphy3

    return pymorphy3.MorphAnalyzer()


def tokens(text: str) -> list[str]:
    """Слова текста в нижнем регистре, «ё» → «е». Составные «2.6.1», «air-flow» дробятся и сохраняются целиком."""
    out: list[str] = []
    for m in _TOKEN_RE.finditer(text.casefold().replace("ё", "е")):
        word = m.group(0)
        if "-" in word or "." in word:
            parts = re.split(r"[.\-]", word)
            if not all(p.isdigit() for p in parts):
                out.extend(parts)
                continue
        out.append(word)
    return out


@lru_cache(maxsize=200_000)
def lemma(word: str) -> str:
    """Нормальная форма слова (первый разбор pymorphy3). Не кириллица — без изменений."""
    if not re.search("[а-я]", word):
        return word
    return _analyzer().parse(word)[0].normal_form.replace("ё", "е")


def lemmas(text: str, *, keep_stopwords: bool = False) -> list[str]:
    """Леммы значимых слов текста в порядке появления (с повторами — для частот BM25)."""
    return [lemma(w) for w in tokens(text) if (keep_stopwords or w not in STOPWORDS) and len(w) > 1]
