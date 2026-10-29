"""Пути и загрузка конфигурации (план §4, ADR 0007).

Корень репозитория — папка app/. Данные организаторов лежат вне git: по умолчанию <app>/data (ссылка на ../data),
иначе — переменная DMS_DATA_DIR. Конфиги — только из <app>/config (п. 5 правил).
"""
from __future__ import annotations

import os
from functools import cache
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = Path(os.environ.get("DMS_CONFIG_DIR", "") or REPO_ROOT / "config")

# Все конфиги, которые обязаны существовать и читаться (проверка — tests/test_skeleton.py, verify.sh)
CONFIG_FILES = (
    "precedence.yaml",
    "verdict_rules.yaml",
    "add_clinic.yaml",
    "care_types.yaml",
    "required_facts.yaml",
    "forbidden_promises.yaml",
    "doc_types.yaml",
    "coverage_markers.yaml",
    "route_examples.yaml",
    "route_markers.yaml",
    "texts_ru/ui.yaml",
    "texts_ru/drafts.yaml",
    "texts_ru/clinic_pick.yaml",
    "texts_ru/mapping.yaml",
    "service_classes.yaml",
    "texts_ru/care_pick.yaml",
    "texts_ru/reply.yaml",
)


def data_dir() -> Path:
    return Path(os.environ.get("DMS_DATA_DIR", "") or REPO_ROOT / "data")


def raw_dir() -> Path:
    """Комплект организаторов (только чтение)."""
    return data_dir() / "raw"


def kit_dir() -> Path:
    """Папка «Документы» комплекта: ГК N / Компания M / файлы."""
    return raw_dir() / "Кейсы_обезличенные" / "Документы"


def gold_xlsx() -> Path:
    return raw_dir() / "Кейсы_обезличенные" / "Кейсы_К10-К19_обезличенные.xlsx"


def derived_dir() -> Path:
    """Результаты разбора и индексы (вне git)."""
    return data_dir() / "derived"


def app_data_dir() -> Path:
    """SQLite сервиса: чаты, запросы, черновики (вне git)."""
    return data_dir() / "app"


@cache
def load_yaml(name: str) -> Any:
    """Конфиг из config/<name>. Ошибка чтения — исключение: без конфига выводы не строятся."""
    path = CONFIG_DIR / name
    with path.open(encoding="utf-8") as fh:
        return yaml.safe_load(fh)


@cache
def prompt(name: str) -> str:
    """Текст промпта config/prompts/<name>.md (версия — в первой строке). Кэш — как у load_yaml: конфиг на ходу
    не меняется (версия промпта читалась с диска дважды на каждый договор ответа)."""
    return (CONFIG_DIR / "prompts" / f"{name}.md").read_text(encoding="utf-8")


def prompt_version(name: str) -> str:
    """Версия промпта из шапки «<!-- version: X -->»."""
    text = prompt(name)
    first = text.splitlines()[0] if text else ""
    if "version:" in first:
        return first.split("version:", 1)[1].strip(" ->")
    return "unknown"
