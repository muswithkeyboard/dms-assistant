"""Адаптеры форматов страховщиков: zetta, renaissance, sogaz, generic (план §6.2, docs/CONTRACTS.md §3).

Выбор адаптера — по признакам документа (СК в преамбуле, структура таблиц), не по имени папки: каждый модуль
адаптера сообщает `matches(kc, root)`; первый подошедший в порядке ADAPTERS разбирает договор, иначе — generic.
"""
from __future__ import annotations

import importlib
from pathlib import Path

from ..constants import Insurer
from ..kit import KitContract
from .base import ParsedContract
from .group_docs import parse_group_docs

# Порядок проверки: от самых узнаваемых признаков к запасному generic
ADAPTERS = ("zetta", "renaissance", "sogaz")


def _module(name: str):
    try:
        return importlib.import_module(f"{__name__}.{name}")
    except ModuleNotFoundError as e:
        if e.name == f"{__name__}.{name}":
            return None        # адаптер ещё не написан (волна 1 идёт параллельно)
        raise


def detect(kc: KitContract, root: Path) -> Insurer:
    for name in ADAPTERS:
        mod = _module(name)
        if mod is not None and mod.matches(kc, root):
            return Insurer(name)
    return Insurer.unknown


def parse_contract(kc: KitContract, root: Path) -> ParsedContract:
    insurer = detect(kc, root)
    name = insurer.value if insurer is not Insurer.unknown else "generic"
    mod = _module(name)
    if mod is None:
        raise RuntimeError(f"нет адаптера {name} для договора {kc.id}")
    return mod.parse(kc, root)


__all__ = ["ADAPTERS", "ParsedContract", "detect", "parse_contract", "parse_group_docs"]
