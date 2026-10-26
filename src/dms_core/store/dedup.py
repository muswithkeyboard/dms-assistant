"""Дедуп текстов пунктов (план §4, §6.2): одинаковый текст хранится в таблице texts один раз.

Договоры внутри ГК совпадают на 87–99 % (`docs/rules.md` п. 0), у Ренессанса 15 вариантов условий на ГК — без дедупа
база раздувается в разы. Пункт (таблица clauses) хранит только ключ текста, а doc_id и contract_id — свои:
ссылка всегда ведёт на документ СВОЕГО договора, общий только байтовый текст (ADR 0005).

Ключ — sha256 ТОЧНОГО текста, а не `Clause.text_hash` (тот нормализует регистр и пробелы для сравнения
вариантов): цитата в citations.verify сверяется с текстом пункта посимвольно, поэтому подменять текст
«почти таким же» нельзя.
"""
from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass


def storage_key(text: str) -> str:
    """Ключ хранения: sha256 точного текста (32 hex — коллизии на объёмах комплекта исключены)."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:32]


def put_texts(conn: sqlite3.Connection, texts: list[str]) -> list[str]:
    """Кладёт тексты в texts (уже имеющиеся не дублируются) и возвращает их ключи в том же порядке."""
    keys = [storage_key(t) for t in texts]
    conn.executemany("INSERT OR IGNORE INTO texts(key, text) VALUES (?, ?)", zip(keys, texts, strict=True))
    return keys


def drop_orphans(conn: sqlite3.Connection) -> int:
    """Удаляет тексты, на которые не ссылается ни один пункт (после замены договора). Возвращает число строк."""
    cur = conn.execute("DELETE FROM texts WHERE key NOT IN (SELECT DISTINCT text_key FROM clauses)")
    return cur.rowcount


@dataclass(frozen=True)
class DedupStats:
    clauses: int          # пунктов всего
    unique_texts: int     # различных текстов в texts
    chars_total: int      # знаков, если бы каждый пункт хранил свой текст
    chars_stored: int     # знаков фактически в texts

    @property
    def chars_saved(self) -> int:
        return self.chars_total - self.chars_stored

    @property
    def saved_ratio(self) -> float:
        return self.chars_saved / self.chars_total if self.chars_total else 0.0


def stats(conn: sqlite3.Connection) -> DedupStats:
    """Метрика дедупа: сколько знаков сэкономлено (по всей базе, без текстов документов в выводе)."""
    clauses, chars_total = conn.execute(
        "SELECT COUNT(*), COALESCE(SUM(LENGTH(t.text)), 0) FROM clauses c JOIN texts t ON t.key = c.text_key"
    ).fetchone()
    unique, chars_stored = conn.execute("SELECT COUNT(*), COALESCE(SUM(LENGTH(text)), 0) FROM texts").fetchone()
    return DedupStats(clauses=clauses, unique_texts=unique, chars_total=chars_total, chars_stored=chars_stored)
