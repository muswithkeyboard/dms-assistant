"""Маршрутизация вопроса по сценарию kNN по эмбеддингам rubert-tiny2 (ADR 0009).

Примеры — config/route_examples.yaml (свои формулировки, не вопросы эталонов). Голосуют k ближайших примеров
по сумме косинусного сходства; соседи ниже min_similarity не голосуют. Нет голосов или нет энкодера — None,
решение остаётся за правилами (route.rule_scenario). Векторы примеров считаются один раз на процесс.
"""
from __future__ import annotations

from dataclasses import dataclass
from functools import cache

from ..config import load_yaml
from ..constants import Scenario
from ..index.embed import get_encoder


@dataclass
class KnnVote:
    scenario: Scenario
    score: float           # сумма сходства голосов за победителя
    top: float             # сходство ближайшего примера
    margin: float          # отрыв победителя от второго места (доля суммы голосов)


@cache
def _bank():
    enc = get_encoder()
    if enc is None:
        return None
    cfg = load_yaml("route_examples.yaml")
    labels, texts = [], []
    for scen, items in cfg["examples"].items():
        for t in items:
            labels.append(Scenario(scen))
            texts.append(t)
    return enc, enc.encode(texts), labels, int(cfg.get("k", 3)), float(cfg.get("min_similarity", 0.55))


def knn_scenario(question: str) -> KnnVote | None:
    bank = _bank()
    if bank is None or not (question or "").strip():
        return None
    enc, vecs, labels, k, floor = bank
    sims = vecs @ enc.encode([question])[0]
    order = sims.argsort()[::-1][:k]
    votes: dict[Scenario, float] = {}
    for i in order:
        if sims[i] >= floor:
            votes[labels[i]] = votes.get(labels[i], 0.0) + float(sims[i])
    if not votes:
        return None
    ranked = sorted(votes.items(), key=lambda kv: kv[1], reverse=True)
    total = sum(votes.values())
    second = ranked[1][1] if len(ranked) > 1 else 0.0
    return KnnVote(ranked[0][0], ranked[0][1], float(sims[order[0]]), (ranked[0][1] - second) / total)
