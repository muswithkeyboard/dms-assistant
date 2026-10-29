"""Хранилище разобранного комплекта: SQLite в data/derived/kit.sqlite (план §4, docs/CONTRACTS.md §4).

Изоляция договоров (ADR 0005) обеспечивается здесь, кодом:
- каждое чтение условий принимает `contract_id` и первым делом зовёт `_require_contract_id` (пустой/не строка →
  ValueError); в SQL всегда стоит `WHERE contract_id = ?` — строки чужого договора выбрать нельзя;
- общие документы ГК (`contract_id IS NULL`: правила СК, каталог) хранятся, но условие `contract_id = ?` их не
  выбирает — в инструменты договора они не попадают (план §1 п. 4, `docs/rules.md` п. 0а); читать их можно только
  отдельным `group_documents(group_id)`;
- `save()` идемпотентен: в одной транзакции удаляет всё прежнее по договору и пишет заново.

Тексты пунктов — через store/dedup.py (одинаковый текст хранится один раз, ссылка остаётся на свой документ).
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Self

from ..config import derived_dir
from ..constants import CLAUSE_CACHE_MAX, DocStatus
from ..schemas import CareMatrix, Clause, ClinicRow, Contract, Document, Group
from . import dedup
from .cache import BoundedCache

_SCHEMA = """
CREATE TABLE IF NOT EXISTS groups (id TEXT PRIMARY KEY, name TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS contracts (
    id TEXT PRIMARY KEY, group_id TEXT NOT NULL, revision INTEGER NOT NULL, card TEXT NOT NULL,
    warnings TEXT NOT NULL DEFAULT '[]');
CREATE TABLE IF NOT EXISTS documents (
    id TEXT PRIMARY KEY, contract_id TEXT, group_id TEXT, doc_type TEXT NOT NULL, ord INTEGER NOT NULL,
    body TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS documents_contract ON documents(contract_id);
CREATE INDEX IF NOT EXISTS documents_group ON documents(group_id);
CREATE TABLE IF NOT EXISTS texts (key TEXT PRIMARY KEY, text TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS clauses (
    id TEXT PRIMARY KEY, doc_id TEXT NOT NULL, contract_id TEXT, group_id TEXT, ord INTEGER NOT NULL,
    number TEXT, path TEXT NOT NULL, program TEXT, text_key TEXT NOT NULL, text_hash TEXT NOT NULL,
    footnotes TEXT NOT NULL, page INTEGER);
CREATE INDEX IF NOT EXISTS clauses_contract ON clauses(contract_id, ord);
CREATE INDEX IF NOT EXISTS clauses_group ON clauses(group_id);
CREATE INDEX IF NOT EXISTS clauses_number ON clauses(contract_id, number);
CREATE TABLE IF NOT EXISTS clinics (
    id TEXT PRIMARY KEY, contract_id TEXT NOT NULL, doc_id TEXT NOT NULL, ord INTEGER NOT NULL,
    program TEXT NOT NULL, care_type TEXT NOT NULL, body TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS clinics_contract ON clinics(contract_id, program);
CREATE TABLE IF NOT EXISTS care (
    contract_id TEXT NOT NULL, ord INTEGER NOT NULL, program TEXT NOT NULL, care_type TEXT NOT NULL,
    body TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS care_contract ON care(contract_id, program);
"""

# Простые фильтры KitStore.clinics(): поле ClinicRow → колонка/проверка. Нечёткие (имя, город, адрес) —
# в tools/clinics.py, здесь только точное совпадение.
_CLINIC_FILTERS = ("program", "care_type", "doc_id", "city", "franchise_pct", "by_referral", "code")


PROGRAM_SEP = ", "   # общий блок нескольких программ: Clause.program = «Стандарт, Бизнес» (docs/CONTRACTS.md §1)


def program_list(value: str | None) -> list[str]:
    """«Стандарт, Бизнес» → ["Стандарт", "Бизнес"]; None — пункт не привязан к программе."""
    if value is None:
        return []
    return [p.strip() for p in value.split(PROGRAM_SEP) if p.strip()]


def program_matches(value: str | None, program: str) -> bool:
    """Значение поля program (одна программа или перечень через «, ») относится к программе program."""
    return program.strip() in program_list(value)


def default_path() -> Path:
    return derived_dir() / "kit.sqlite"


def _require_contract_id(contract_id: object) -> str:
    """ADR 0005: без contract_id чтение условий запрещено (иначе можно смешать договоры ГК)."""
    if not isinstance(contract_id, str) or not contract_id.strip():
        raise ValueError("нужен contract_id: условия читаются только в рамках одного договора (ADR 0005)")
    return contract_id


class ContractNotLoaded(KeyError):
    """Договора нет в базе комплекта (не импортирован или удалён). Отдельный класс (ревью 29.10.2026 п. 3): сервис
    отвечает «договор не загружен» только на него, а не на любую KeyError внутри движка."""


class KitStore:
    """Разобранный комплект. Одно соединение на объект, доступ под блокировкой (сервис многопоточный)."""

    def __init__(self, path: Path | str | None = None) -> None:
        self.path = Path(path) if path is not None else default_path()
        if str(self.path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        # пункты договора по метке ревизии (круг 2 ревизии w16): ответ по одному договору читал их 3–5 раз
        # (guided, разделы, verify), в ГК 2 — по ~47 тыс. пунктов; save() меняет ревизию — кэш устаревает сам
        self._clause_cache: BoundedCache[tuple[int | None, list[Clause]]] = BoundedCache(
            CLAUSE_CACHE_MAX, lambda v: len(v[1]))
        # один объект строки на одинаковый текст / путь: договоры ГК совпадают на 87–99 % (весь комплект — 61 тыс.
        # пунктов и 3,6 тыс. разных текстов); пул — по ключу текста хранилища, растёт до числа разных текстов
        self._pool: dict[str, str] = {}
        self._conn = self._open()

    def _open(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.path), check_same_thread=False)
        conn.execute("PRAGMA foreign_keys = ON")
        if str(self.path) != ":memory:":
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("PRAGMA busy_timeout = 5000")
        conn.executescript(_SCHEMA)
        conn.commit()
        return conn

    # ------------------------------------------------------------------ служебное

    def replace_file(self, new_file: Path, target: Path | None = None) -> None:
        """Заменить базу собранным файлом (импорт комплекта из интерфейса, ревью 29.10.2026 п. 1): под блокировкой
        хранилища — сброс журнала WAL, закрытие соединения, атомарный os.replace, новое соединение. Объект тот же:
        сервис, движок и default_store() видят новую базу сразу, недописанную — никогда (сборка шла в new_file).
        target — путь базы, если хранилище было в памяти (чистая установка): дальше оно живёт в этом файле."""
        dest = Path(target) if target is not None else self.path
        if str(dest) == ":memory:":
            raise ValueError("хранилище в памяти: укажите файл базы (target)")
        with self._lock:
            if str(self.path) != ":memory:":
                self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            self._conn.close()
            # журнал WAL старой базы не должен достаться новому файлу
            for suffix in ("-wal", "-shm"):
                dest.with_name(dest.name + suffix).unlink(missing_ok=True)
            dest.parent.mkdir(parents=True, exist_ok=True)
            os.replace(new_file, dest)
            self.path = dest
            self._conn = self._open()
            for key in self._clause_cache.snapshot():
                self._clause_cache.pop(key)
            self._pool.clear()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            try:
                yield self._conn
                self._conn.commit()
            except BaseException:
                self._conn.rollback()
                raise

    def _rows(self, sql: str, params: Iterable[Any] = ()) -> list[tuple]:
        with self._lock:
            return self._conn.execute(sql, tuple(params)).fetchall()

    # ------------------------------------------------------------------ запись

    def save(self, parsed: Any) -> None:
        """Записывает договор (adapters.ParsedContract). Повторный вызов полностью заменяет договор.

        Проверка изоляции на входе: каждый документ, пункт, клиника и строка матрицы обязаны принадлежать этому
        договору (docs/CONTRACTS.md §1). Общие документы ГК (contract_id=None) пишутся через save_group().
        """
        c: Contract = parsed.contract
        cid = _require_contract_id(c.id)
        own_docs = {d.id for d in parsed.documents}
        for d in parsed.documents:
            if d.contract_id != cid:
                raise ValueError(f"документ {d.id} не принадлежит договору {cid}")
        for cl in parsed.clauses:
            if cl.contract_id != cid or cl.doc_id not in own_docs:
                raise ValueError(f"пункт {cl.id} не принадлежит договору {cid}")
        for r in parsed.clinics:
            if r.contract_id != cid or r.doc_id not in own_docs:
                raise ValueError(f"строка клиник {r.id} не принадлежит договору {cid}")
        for m in parsed.care:
            if m.contract_id != cid:
                raise ValueError(f"строка матрицы {m.program}/{m.care_type} не принадлежит договору {cid}")

        self._pool.clear()                   # переимпорт: старые тексты договора пулом не удерживаются
        with self._tx() as conn:
            self._delete_contract(conn, cid)
            conn.execute("INSERT OR IGNORE INTO groups(id, name) VALUES (?, ?)",
                         (c.group_id, _group_name(c.group_id)))
            conn.execute(
                "INSERT INTO contracts(id, group_id, revision, card, warnings) VALUES (?, ?, ?, ?, ?)",
                (cid, c.group_id, time.time_ns(), c.model_dump_json(), json.dumps(list(parsed.warnings),
                                                                                  ensure_ascii=False)))
            self._insert_documents(conn, parsed.documents)
            self._insert_clauses(conn, parsed.clauses, group_id=None)
            conn.executemany(
                "INSERT INTO clinics(id, contract_id, doc_id, ord, program, care_type, body) VALUES (?,?,?,?,?,?,?)",
                [(r.id, cid, r.doc_id, i, r.program, r.care_type, r.model_dump_json())
                 for i, r in enumerate(parsed.clinics)])
            conn.executemany(
                "INSERT INTO care(contract_id, ord, program, care_type, body) VALUES (?,?,?,?,?)",
                [(cid, i, m.program, m.care_type, m.model_dump_json()) for i, m in enumerate(parsed.care)])
            dedup.drop_orphans(conn)

    def save_group(self, group: Group, documents: list[Document], clauses: list[Clause]) -> None:
        """Общие документы ГК (правила СК, каталог): contract_id=None. Идемпотентно заменяет документы группы."""
        for d in documents:
            if d.contract_id is not None or d.group_id != group.id:
                raise ValueError(f"документ {d.id} не общий документ группы {group.id}")
        ids = {d.id for d in documents}
        for cl in clauses:
            if cl.contract_id is not None or cl.doc_id not in ids:
                raise ValueError(f"пункт {cl.id} не относится к общим документам группы {group.id}")
        with self._tx() as conn:
            conn.execute("INSERT OR REPLACE INTO groups(id, name) VALUES (?, ?)", (group.id, group.name))
            conn.execute("DELETE FROM clauses WHERE contract_id IS NULL AND group_id = ?", (group.id,))
            conn.execute("DELETE FROM documents WHERE contract_id IS NULL AND group_id = ?", (group.id,))
            self._insert_documents(conn, documents)
            self._insert_clauses(conn, clauses, group_id=group.id)
            dedup.drop_orphans(conn)

    def add_documents(self, contract_id: str, documents: list[Document], clauses: list[Clause]) -> None:
        """Дописать документы и их пункты к уже загруженному договору (документ из чата, ADR 0016) и сменить
        ревизию договора: кэш пунктов (_clause_cache) и индекс поиска (index/search.get_index) пересоберутся по
        метке ревизии сами. Изоляция — как в save(): документы и пункты только этого договора, пункты — только
        добавляемых документов; id документа не должен совпадать с уже загруженным. Повторный save() договора
        (импорт комплекта, обработка договора клиента) такие документы стирает."""
        cid = self._known(contract_id)
        own_docs = {d.id for d in documents}
        for d in documents:
            if d.contract_id != cid:
                raise ValueError(f"документ {d.id} не принадлежит договору {cid}")
        for cl in clauses:
            if cl.contract_id != cid or cl.doc_id not in own_docs:
                raise ValueError(f"пункт {cl.id} не принадлежит договору {cid}")
        with self._tx() as conn:
            if own_docs and conn.execute(
                    f"SELECT 1 FROM documents WHERE id IN ({','.join('?' * len(own_docs))})",
                    tuple(own_docs)).fetchone():
                raise ValueError("документ с таким id уже загружен")
            doc_start = conn.execute("SELECT COALESCE(MAX(ord), -1) + 1 FROM documents WHERE contract_id = ?",
                                     (cid,)).fetchone()[0]
            cl_start = conn.execute("SELECT COALESCE(MAX(ord), -1) + 1 FROM clauses WHERE contract_id = ?",
                                    (cid,)).fetchone()[0]
            self._insert_documents(conn, documents, start=doc_start)
            self._insert_clauses(conn, clauses, group_id=None, start=cl_start)
            conn.execute("UPDATE contracts SET revision = ? WHERE id = ?", (time.time_ns(), cid))

    def set_doc_status(self, doc_id: str, status: DocStatus, warning: str | None = None) -> None:
        """Статус готовности документа (ТЗ п. 7.1); предупреждение добавляется к Document.warnings."""
        with self._tx() as conn:
            row = conn.execute("SELECT body FROM documents WHERE id = ?", (doc_id,)).fetchone()
            if row is None:
                raise KeyError(doc_id)
            doc = Document.model_validate_json(row[0])
            doc.status = status
            if warning:
                doc.warnings.append(warning)
            conn.execute("UPDATE documents SET body = ? WHERE id = ?", (doc.model_dump_json(), doc_id))

    def delete_contract(self, contract_id: str) -> None:
        cid = _require_contract_id(contract_id)
        self._clause_cache.pop(cid)
        self._pool.clear()                   # тексты удалённого договора пулом не удерживаются
        with self._tx() as conn:
            self._delete_contract(conn, cid)
            dedup.drop_orphans(conn)

    @staticmethod
    def _delete_contract(conn: sqlite3.Connection, cid: str) -> None:
        for table in ("care", "clinics", "clauses", "documents", "contracts"):
            col = "id" if table == "contracts" else "contract_id"
            conn.execute(f"DELETE FROM {table} WHERE {col} = ?", (cid,))

    @staticmethod
    def _insert_documents(conn: sqlite3.Connection, documents: list[Document], start: int = 0) -> None:
        conn.executemany(
            "INSERT INTO documents(id, contract_id, group_id, doc_type, ord, body) VALUES (?,?,?,?,?,?)",
            [(d.id, d.contract_id, d.group_id, d.doc_type.value, i, d.model_dump_json())
             for i, d in enumerate(documents, start=start)])

    @staticmethod
    def _insert_clauses(conn: sqlite3.Connection, clauses: list[Clause], group_id: str | None,
                        start: int = 0) -> None:
        keys = dedup.put_texts(conn, [cl.text for cl in clauses])
        conn.executemany(
            "INSERT INTO clauses(id, doc_id, contract_id, group_id, ord, number, path, program, text_key, "
            "text_hash, footnotes, page) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            [(cl.id, cl.doc_id, cl.contract_id, group_id, i, cl.number, cl.path, cl.program, key, cl.text_hash,
              json.dumps(cl.footnotes, ensure_ascii=False), cl.page)
             for i, (cl, key) in enumerate(zip(clauses, keys, strict=True), start=start)])

    # ------------------------------------------------------------------ чтение: группы и договоры

    def groups(self) -> list[Group]:
        return [Group(id=i, name=n) for i, n in self._rows("SELECT id, name FROM groups ORDER BY id")]

    def contracts(self, group_id: str | None = None) -> list[Contract]:
        """Карточки договоров (список для выбора контекста; условий не содержит)."""
        if group_id is None:
            rows = self._rows("SELECT card FROM contracts ORDER BY id")
        else:
            rows = self._rows("SELECT card FROM contracts WHERE group_id = ? ORDER BY id", (group_id,))
        return [Contract.model_validate_json(r[0]) for r in rows]

    def _known(self, contract_id: str) -> str:
        """contract_id загруженного договора; иначе KeyError. Пустой ответ по несуществующему договору агент прочитал
        бы как «клиник / пунктов нет» — это ложное «нет в документах» (`docs/rules.md` п. 3а)."""
        cid = _require_contract_id(contract_id)
        if not self._rows("SELECT 1 FROM contracts WHERE id = ?", (cid,)):
            raise ContractNotLoaded(f"договор {cid} не загружен")
        return cid

    def contract(self, contract_id: str) -> Contract:
        cid = _require_contract_id(contract_id)
        rows = self._rows("SELECT card FROM contracts WHERE id = ?", (cid,))
        if not rows:
            raise ContractNotLoaded(f"договор {cid} не загружен")
        return Contract.model_validate_json(rows[0][0])

    def contract_warnings(self, contract_id: str) -> list[str]:
        cid = _require_contract_id(contract_id)
        rows = self._rows("SELECT warnings FROM contracts WHERE id = ?", (cid,))
        return json.loads(rows[0][0]) if rows else []

    def revision(self, contract_id: str) -> int | None:
        """Метка последнего save() договора — по ней index/search.py сбрасывает кэш индекса."""
        cid = _require_contract_id(contract_id)
        rows = self._rows("SELECT revision FROM contracts WHERE id = ?", (cid,))
        return rows[0][0] if rows else None

    # ------------------------------------------------------------------ чтение: документы и пункты договора

    def documents(self, contract_id: str) -> list[Document]:
        cid = self._known(contract_id)
        rows = self._rows("SELECT body FROM documents WHERE contract_id = ? ORDER BY ord", (cid,))
        return [Document.model_validate_json(r[0]) for r in rows]

    def group_documents(self, group_id: str) -> list[Document]:
        """Общие документы ГК (правила СК, каталог) — НЕ условия договора, только для показа «что загружено»."""
        if not group_id:
            raise ValueError("нужен group_id")
        rows = self._rows("SELECT body FROM documents WHERE contract_id IS NULL AND group_id = ? ORDER BY ord",
                          (group_id,))
        return [Document.model_validate_json(r[0]) for r in rows]

    _CLAUSE_SQL = ("SELECT c.id, c.doc_id, c.contract_id, c.number, c.path, c.program, t.text, c.footnotes, c.page, "
                   "c.text_hash FROM clauses c JOIN texts t ON t.key = c.text_key ")

    @staticmethod
    def _clause(row: tuple) -> Clause:
        cid_, doc_id, contract_id, number, path, program, text, footnotes, page, th = row
        return Clause(id=cid_, doc_id=doc_id, contract_id=contract_id, number=number, path=path, program=program,
                      text=text, footnotes=json.loads(footnotes), page=page, text_hash=th)

    def _pooled(self, row: tuple) -> Clause:
        """Пункт из строки базы с общими объектами строк текста и пути (_pool)."""
        cid_, doc_id, contract_id, number, path, program, text, footnotes, page, th, key = row
        pool = self._pool
        if len(pool) > 4 * CLAUSE_CACHE_MAX:
            pool.clear()                     # пул не держит тексты давно вытесненных договоров сверх предела кэша
        text = pool.setdefault("t" + key, text)
        # повторяющиеся строки (путь, документ, программа, номер, хэш текста) — тоже общие объекты
        path, doc_id, th = pool.setdefault("p" + path, path), pool.setdefault("d" + doc_id, doc_id), \
            pool.setdefault("h" + th, th)
        program = pool.setdefault("g" + program, program) if program else program
        number = pool.setdefault("n" + number, number) if number else number
        return Clause(id=cid_, doc_id=doc_id, contract_id=contract_id, number=number, path=path, program=program,
                      text=text, footnotes=json.loads(footnotes) if footnotes != "[]" else [], page=page,
                      text_hash=th)

    def clauses(self, contract_id: str, doc_types: Iterable[str] | None = None) -> list[Clause]:
        """Пункты документов договора в порядке документа. doc_types — фильтр по DocType (значения строк).
        Пункты — общие объекты кэша: их не изменять (список — свой у каждого вызова)."""
        cid = self._known(contract_id)
        rev = self.revision(cid)
        got = self._clause_cache.get(cid)
        if got is None or got[0] != rev:
            with self._lock:
                rows = self._rows(self._CLAUSE_SQL.replace(" FROM", ", t.key FROM", 1)
                                  + "WHERE c.contract_id = ? ORDER BY c.ord", [cid])
                got = (rev, [self._pooled(r) for r in rows])
            self._clause_cache.put(cid, got)     # предел памяти — store/cache.py
        if doc_types is None:
            return list(got[1])
        types = {str(getattr(t, "value", t)) for t in doc_types}
        docs = {d.id for d in self.documents(cid) if d.doc_type.value in types}
        return [c for c in got[1] if c.doc_id in docs]

    def clause(self, contract_id: str, number: str | None = None, path: str | None = None) -> list[Clause]:
        """Пункт по номеру («2.6.1») или пути. Номера в разных документах договора повторяются — список.

        Путь: сначала точное совпадение, затем окончание пути (« › 1.4» в программе Ренессанса).
        """
        cid = self._known(contract_id)
        if number is None and path is None:
            raise ValueError("нужен номер или путь пункта")
        if path is not None:
            path = path.strip()
            if not path:
                raise ValueError("пустой путь пункта")
            rows = self._rows(self._CLAUSE_SQL + "WHERE c.contract_id = ? AND c.path = ? ORDER BY c.ord", (cid, path))
            if not rows:
                # окончание пути — только целыми сегментами после «› »: «1.4» не находит «3.1.4»
                rows = self._rows(self._CLAUSE_SQL + "WHERE c.contract_id = ? AND c.path LIKE ? ESCAPE '\\' "
                                  "ORDER BY c.ord", (cid, "%› " + _like_escape(path)))
        else:
            # Номер — как показывает Word: у Зетты «1.2.», у СОГАЗ и Ренессанса «1.2»; сравниваем без
            # завершающей точки и пробелов, чтобы «п. 1.2» из вопроса или эталона находил оба написания.
            rows = self._rows(self._CLAUSE_SQL + "WHERE c.contract_id = ? AND rtrim(trim(c.number), '.') = ? "
                              "ORDER BY c.ord", (cid, number.strip().rstrip(".")))
        return [self._clause(r) for r in rows]

    # ------------------------------------------------------------------ чтение: клиники и матрица

    def clinics(self, contract_id: str, **filters: Any) -> list[ClinicRow]:
        """Строки перечня клиник договора. Фильтры — точные: program, care_type, doc_id, city, franchise_pct,
        by_referral, code (значение None у фильтра = «не фильтровать»)."""
        cid = self._known(contract_id)
        unknown = set(filters) - set(_CLINIC_FILTERS)
        if unknown:
            raise TypeError(f"неизвестные фильтры клиник: {sorted(unknown)}")
        sql, params = "SELECT body FROM clinics WHERE contract_id = ?", [cid]
        for col in ("care_type", "doc_id"):
            if filters.get(col) is not None:
                sql += f" AND {col} = ?"
                params.append(filters[col])
        rows = [ClinicRow.model_validate_json(r[0]) for r in self._rows(sql + " ORDER BY ord", params)]
        if filters.get("program") is not None:
            # программа строки может быть перечнем через «, » — совпадение с любой (docs/CONTRACTS.md §1)
            rows = [r for r in rows if program_matches(r.program, filters["program"])]
        for name in ("city", "franchise_pct", "by_referral", "code"):
            value = filters.get(name)
            if value is not None:
                rows = [r for r in rows if getattr(r, name) == value]
        return rows

    def care(self, contract_id: str, program: str | None = None) -> list[CareMatrix]:
        cid = self._known(contract_id)
        rows = self._rows("SELECT body FROM care WHERE contract_id = ? ORDER BY ord", (cid,))
        out = [CareMatrix.model_validate_json(r[0]) for r in rows]
        return out if program is None else [m for m in out if program_matches(m.program, program)]

    # ------------------------------------------------------------------ метрики

    def dedup_stats(self) -> dedup.DedupStats:
        with self._lock:
            return dedup.stats(self._conn)

    def size_bytes(self) -> int:
        if str(self.path) == ":memory:":
            page_count = self._rows("PRAGMA page_count")[0][0]
            return page_count * self._rows("PRAGMA page_size")[0][0]
        with self._lock:
            self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        return self.path.stat().st_size


def _group_name(group_id: str) -> str:
    """«gk3» → «ГК 3» (имя по описи, docs/CONTRACTS.md: группа gk<N> по папке «ГК N»)."""
    return f"ГК {group_id[2:]}" if group_id.startswith("gk") else group_id


def _like_escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
