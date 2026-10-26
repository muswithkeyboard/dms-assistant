"""BM25 Okapi без зависимостей (план §4): обратный индекс «лемма → (документ, частота)».

Запрос — словарь «лемма → вес»: так расширение синонимами (index/search.py) получает пониженный вес,
а слова исходного запроса — полный.
"""
from __future__ import annotations

import math
from collections import Counter, defaultdict
from collections.abc import Sequence

K1 = 1.5     # насыщение частоты термина (стандартное значение Okapi)
B = 0.75     # нормализация по длине документа


class BM25:
    def __init__(self, docs: Sequence[Sequence[str]]) -> None:
        self.n = len(docs)
        self.lengths = [len(d) for d in docs]
        self.avgdl = (sum(self.lengths) / self.n) if self.n else 0.0
        postings: dict[str, list[tuple[int, int]]] = defaultdict(list)
        for i, doc in enumerate(docs):
            for term, tf in Counter(doc).items():
                postings[term].append((i, tf))
        self.postings = dict(postings)
        self.idf = {t: math.log(1.0 + (self.n - len(p) + 0.5) / (len(p) + 0.5)) for t, p in self.postings.items()}

    def scores(self, query: dict[str, float], allowed: set[int] | None = None) -> dict[int, float]:
        """Оценки документов, где встретился хотя бы один термин запроса. allowed — допустимые номера документов."""
        acc: dict[int, float] = defaultdict(float)
        for term, weight in query.items():
            plist = self.postings.get(term)
            if not plist or weight <= 0:
                continue
            idf = self.idf[term] * weight
            for i, tf in plist:
                if allowed is not None and i not in allowed:
                    continue
                norm = K1 * (1 - B + B * self.lengths[i] / self.avgdl) if self.avgdl else K1
                acc[i] += idf * tf * (K1 + 1) / (tf + norm)
        return dict(acc)

    def top(self, query: dict[str, float], k: int, allowed: set[int] | None = None) -> list[tuple[int, float]]:
        s = self.scores(query, allowed)
        return sorted(s.items(), key=lambda kv: (-kv[1], kv[0]))[:k]
