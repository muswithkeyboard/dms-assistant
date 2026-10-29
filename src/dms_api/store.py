"""Хранилище сервиса: чаты, запросы, сообщения, черновики, загруженные документы (CONTRACTS §8).

SQLite в data/app/app.sqlite (вне git). Здесь же реестр клиентов и заданий подключения (ADR 0011): AppStore
реализует протокол dms_core.onboarding.Registry. Каждая запись — JSON модели schemas.py (`model_dump_json`), рядом —
столбцы для выборок (чат запроса, статус, порядок). Схема — по номеру версии `PRAGMA user_version`: при открытии
недостающие шаги MIGRATIONS выполняются по порядку в одной транзакции (старт сервиса = миграция).

Сообщения запроса только дописываются (`add_message`): два ответа подряд не затирают друг друга, даже если
параллельно идёт шаг автомата (save_request пишет карточку и черновики, а из сообщений — только новые).
Тексты документов здесь не хранятся: у загруженного файла — только метаданные Document. Исключение — вложения чата
(ADR 0016): пункты прикреплённого к чату документа хранятся здесь (attachment_clauses), чтобы отвечать по нему и
открывать пункт по ссылке; база — в data/app/, вне репозитория; в журнал тексты не пишутся.
"""
from __future__ import annotations

import datetime as dt
import json
import secrets
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from dms_core.constants import CLIENT_CONTRACT_PREFIX, CLIENT_GROUP_PREFIX, PROFILE_PREFIX, Insurer, RequestStatus
from dms_core.schemas import (
    Chat,
    ChatContext,
    Clause,
    ClientContract,
    ClientFile,
    ClientGroup,
    Document,
    Draft,
    InsurerProfile,
    Message,
    OnboardingJob,
    Request,
    TableMapping,
    TableRef,
)

# Шаги схемы: индекс + 1 = user_version после шага. Новые шаги — только в конец списка.
MIGRATIONS: list[str] = [
    """
    CREATE TABLE chats (id TEXT PRIMARY KEY, created_at TEXT NOT NULL, body TEXT NOT NULL);
    CREATE TABLE requests (id TEXT PRIMARY KEY, chat_id TEXT NOT NULL REFERENCES chats(id), status TEXT NOT NULL,
                           ord INTEGER NOT NULL, body TEXT NOT NULL);
    CREATE INDEX requests_chat ON requests(chat_id, status);
    CREATE TABLE messages (request_id TEXT NOT NULL REFERENCES requests(id), seq INTEGER NOT NULL,
                           body TEXT NOT NULL, PRIMARY KEY (request_id, seq));
    CREATE TABLE drafts (id TEXT PRIMARY KEY, request_id TEXT NOT NULL REFERENCES requests(id), ord INTEGER NOT NULL,
                         body TEXT NOT NULL);
    CREATE INDEX drafts_request ON drafts(request_id);
    CREATE TABLE uploads (id TEXT PRIMARY KEY, contract_id TEXT, group_id TEXT, ord INTEGER NOT NULL,
                          body TEXT NOT NULL);
    """,
    # 2: подключение клиента (ADR 0011) — реестр групп, договоров, файлов клиента и задания обработки
    """
    CREATE TABLE client_groups (id TEXT PRIMARY KEY, n INTEGER NOT NULL UNIQUE, body TEXT NOT NULL);
    CREATE TABLE client_contracts (id TEXT PRIMARY KEY, group_id TEXT NOT NULL REFERENCES client_groups(id),
                                   n INTEGER NOT NULL UNIQUE, body TEXT NOT NULL);
    CREATE INDEX client_contracts_group ON client_contracts(group_id, n);
    CREATE TABLE client_files (owner_id TEXT NOT NULL, name TEXT NOT NULL, ord INTEGER NOT NULL, body TEXT NOT NULL,
                               PRIMARY KEY (owner_id, name));
    CREATE TABLE onboarding_jobs (id TEXT PRIMARY KEY, contract_id TEXT NOT NULL, ord INTEGER NOT NULL,
                                  body TEXT NOT NULL);
    CREATE INDEX onboarding_jobs_contract ON onboarding_jobs(contract_id, ord);
    """,
    # 3: снимок файлов последней успешной обработки договора клиента (ревизия w8, пп. 6–7): по нему импорт комплекта
    # повторяет обработанную версию, а GET /api/clients показывает «файлы изменены»
    """
    CREATE TABLE client_processed (contract_id TEXT PRIMARY KEY, body TEXT NOT NULL);
    """,
    # 4: разметка таблиц оператором (ADR 0012) — разметки договора (ref — ключ таблицы JSON) и профили СК
    # (подпись заголовка → разметка колонок; данных документов нет)
    """
    CREATE TABLE table_mappings (contract_id TEXT NOT NULL, ref TEXT NOT NULL, ord INTEGER NOT NULL,
                                 body TEXT NOT NULL, PRIMARY KEY (contract_id, ref));
    CREATE TABLE insurer_profiles (id TEXT PRIMARY KEY, n INTEGER NOT NULL UNIQUE, signature TEXT NOT NULL UNIQUE,
                                   body TEXT NOT NULL);
    """,
    # 5: какие договоры применили профиль СК (w9-fix п. 2: счётчик used — по договорам, не по таблицам и не по
    # повторным обработкам одного договора)
    """
    CREATE TABLE profile_uses (profile_id TEXT NOT NULL, contract_id TEXT NOT NULL,
                               PRIMARY KEY (profile_id, contract_id));
    """,
    # 6: вложения чата (ADR 0016) — документ принадлежит чату, его пункты, активное вложение чата; настройки сервиса
    # (ключ → значение JSON)
    """
    CREATE TABLE chat_attachments (id TEXT PRIMARY KEY,
                                   chat_id TEXT NOT NULL REFERENCES chats(id) ON DELETE CASCADE,
                                   ord INTEGER NOT NULL, sha256 TEXT NOT NULL, body TEXT NOT NULL);
    CREATE INDEX chat_attachments_chat ON chat_attachments(chat_id, ord);
    CREATE TABLE attachment_clauses (attachment_id TEXT NOT NULL REFERENCES chat_attachments(id) ON DELETE CASCADE,
                                     ord INTEGER NOT NULL, body TEXT NOT NULL, PRIMARY KEY (attachment_id, ord));
    CREATE TABLE chat_active_attachment (chat_id TEXT PRIMARY KEY REFERENCES chats(id) ON DELETE CASCADE,
                                         attachment_id TEXT NOT NULL REFERENCES chat_attachments(id) ON DELETE CASCADE);
    CREATE TABLE app_settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
    """,
]


def new_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(6)}"


def now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


class AppStore:
    """Одно соединение на объект под блокировкой (сервис многопоточный: ответы считаются в потоках)."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path) if str(path) != ":memory:" else path
        if isinstance(self.path, Path):
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.execute("PRAGMA foreign_keys = ON")
        if isinstance(self.path, Path):
            self._conn.execute("PRAGMA journal_mode = WAL")
        self.migrate()

    # ------------------------------------------------------------------ служебное

    def migrate(self) -> int:
        """Довести схему до последней версии; вернуть номер версии."""
        with self._lock:
            version = self._conn.execute("PRAGMA user_version").fetchone()[0]
            for n in range(version, len(MIGRATIONS)):
                with self._tx() as conn:
                    conn.executescript("BEGIN;" + MIGRATIONS[n] + f"; PRAGMA user_version = {n + 1};")
            return self._conn.execute("PRAGMA user_version").fetchone()[0]

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            try:
                yield self._conn
                self._conn.commit()
            except BaseException:
                self._conn.rollback()
                raise

    def _one(self, sql: str, params: tuple = ()) -> tuple | None:
        with self._lock:
            return self._conn.execute(sql, params).fetchone()

    def _all(self, sql: str, params: tuple = ()) -> list[tuple]:
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    # ------------------------------------------------------------------ чаты

    def create_chat(self, context: ChatContext, title: str | None) -> Chat:
        chat = Chat(id=new_id("ch"), title=title or "Чат", context=context, created_at=now())
        with self._tx() as conn:
            conn.execute("INSERT INTO chats(id, created_at, body) VALUES (?,?,?)",
                         (chat.id, chat.created_at.isoformat(), chat.model_dump_json()))
        return chat

    def chats(self) -> list[Chat]:
        """Новые сверху."""
        return [Chat.model_validate_json(r[0]) for r in self._all("SELECT body FROM chats ORDER BY created_at DESC")]

    def chat(self, chat_id: str) -> Chat:
        row = self._one("SELECT body FROM chats WHERE id = ?", (chat_id,))
        if row is None:
            raise KeyError(chat_id)
        return Chat.model_validate_json(row[0])

    # ------------------------------------------------------------------ запросы

    def create_request(self, chat_id: str, title: str) -> Request:
        self.chat(chat_id)
        req = Request(id=new_id("rq"), chat_id=chat_id, title=title)
        with self._tx() as conn:
            ord_ = conn.execute("SELECT COALESCE(MAX(ord), 0) + 1 FROM requests").fetchone()[0]
            conn.execute("INSERT INTO requests(id, chat_id, status, ord, body) VALUES (?,?,?,?,?)",
                         (req.id, chat_id, req.status.value, ord_, self._card(req)))
        return req

    @staticmethod
    def _card(req: Request) -> str:
        """Карточка запроса без сообщений и черновиков — они в своих таблицах."""
        return req.model_dump_json(exclude={"messages", "drafts"})

    def _load(self, row: tuple) -> Request:
        rid, body = row
        req = Request.model_validate_json(body)
        req.messages = [Message.model_validate_json(r[0]) for r in
                        self._all("SELECT body FROM messages WHERE request_id = ? ORDER BY seq", (rid,))]
        req.drafts = [Draft.model_validate_json(r[0]) for r in
                      self._all("SELECT body FROM drafts WHERE request_id = ? ORDER BY ord", (rid,))]
        return req

    def requests(self, chat_id: str, status: str = "open") -> list[Request]:
        """status: open | closed | all. Новые сверху."""
        self.chat(chat_id)
        if status == "all":
            rows = self._all("SELECT id, body FROM requests WHERE chat_id = ? ORDER BY ord DESC", (chat_id,))
        else:
            RequestStatus(status)                      # ValueError на неизвестный статус
            rows = self._all("SELECT id, body FROM requests WHERE chat_id = ? AND status = ? ORDER BY ord DESC",
                             (chat_id, status))
        return [self._load(r) for r in rows]

    def request(self, request_id: str) -> Request:
        row = self._one("SELECT id, body FROM requests WHERE id = ?", (request_id,))
        if row is None:
            raise KeyError(request_id)
        return self._load(row)

    def save_request(self, r: Request) -> None:
        """Карточка и черновики — целиком; из сообщений дописываются только те, которых в базе ещё нет.
        Запись целиком: вызывающий обязан держать блокировку запроса и перечитать его под ней
        (Service._locked, ревизия w3), иначе параллельный шаг автомата или оценка затираются."""
        with self._tx() as conn:
            cur = conn.execute("UPDATE requests SET status = ?, body = ? WHERE id = ?",
                               (r.status.value, self._card(r), r.id))
            if cur.rowcount == 0:
                raise KeyError(r.id)
            stored = conn.execute("SELECT COUNT(*) FROM messages WHERE request_id = ?", (r.id,)).fetchone()[0]
            for i, m in enumerate(r.messages[stored:], start=stored):
                conn.execute("INSERT INTO messages(request_id, seq, body) VALUES (?,?,?)",
                             (r.id, i, m.model_dump_json()))
            conn.execute("DELETE FROM drafts WHERE request_id = ?", (r.id,))
            conn.executemany("INSERT INTO drafts(id, request_id, ord, body) VALUES (?,?,?,?)",
                             [(d.id, r.id, i, d.model_dump_json()) for i, d in enumerate(r.drafts)])

    def add_message(self, request_id: str, message: Message) -> None:
        with self._tx() as conn:
            if conn.execute("SELECT 1 FROM requests WHERE id = ?", (request_id,)).fetchone() is None:
                raise KeyError(request_id)
            seq = conn.execute("SELECT COALESCE(MAX(seq), -1) + 1 FROM messages WHERE request_id = ?",
                               (request_id,)).fetchone()[0]
            conn.execute("INSERT INTO messages(request_id, seq, body) VALUES (?,?,?)",
                         (request_id, seq, message.model_dump_json()))

    # ------------------------------------------------------------------ черновики

    def draft(self, draft_id: str) -> tuple[Request, Draft]:
        row = self._one("SELECT request_id FROM drafts WHERE id = ?", (draft_id,))
        if row is None:
            raise KeyError(draft_id)
        req = self.request(row[0])
        return req, next(d for d in req.drafts if d.id == draft_id)

    def save_draft(self, draft: Draft) -> None:
        with self._tx() as conn:
            cur = conn.execute("UPDATE drafts SET body = ? WHERE id = ?", (draft.model_dump_json(), draft.id))
            if cur.rowcount == 0:
                raise KeyError(draft.id)

    # ------------------------------------------------------------------ загруженные документы (метаданные)

    def save_upload(self, doc: Document) -> None:
        with self._tx() as conn:
            ord_ = conn.execute("SELECT COALESCE(MAX(ord), 0) + 1 FROM uploads").fetchone()[0]
            conn.execute("INSERT OR REPLACE INTO uploads(id, contract_id, group_id, ord, body) VALUES (?,?,?,?,?)",
                         (doc.id, doc.contract_id, doc.group_id, ord_, doc.model_dump_json()))

    def uploads(self, contract_id: str | None = None, group_id: str | None = None) -> list[Document]:
        sql, params = "SELECT body FROM uploads WHERE 1=1", []
        if contract_id is not None:
            sql += " AND contract_id = ?"
            params.append(contract_id)
        if group_id is not None:
            sql += " AND group_id = ?"
            params.append(group_id)
        return [Document.model_validate_json(r[0]) for r in self._all(sql + " ORDER BY ord", tuple(params))]

    def upload(self, doc_id: str) -> Document | None:
        row = self._one("SELECT body FROM uploads WHERE id = ?", (doc_id,))
        return Document.model_validate_json(row[0]) if row else None

    # ------------------------------------------------------------------ клиенты (ADR 0011; протокол onboarding.Registry)

    def create_client_group(self, name: str) -> ClientGroup:
        """Новая группа клиента: id ugN — следующий номер (не пересекается с gkN комплекта)."""
        with self._tx() as conn:
            n = conn.execute("SELECT COALESCE(MAX(n), 0) + 1 FROM client_groups").fetchone()[0]
            g = ClientGroup(id=f"{CLIENT_GROUP_PREFIX}{n}", name=name, created_at=now())
            conn.execute("INSERT INTO client_groups(id, n, body) VALUES (?,?,?)",
                         (g.id, n, g.model_dump_json(exclude={"contracts"})))
        return g

    def add_client_contract(self, group_id: str, legal_entity: str, insurer_hint: Insurer | None = None,
                            number: str | None = None, insurer_name: str | None = None,
                            force_generic: bool = False) -> ClientContract:
        """Договор клиента: id uNN — сквозной номер по всем группам (не пересекается с cNN комплекта)."""
        with self._tx() as conn:
            if conn.execute("SELECT 1 FROM client_groups WHERE id = ?", (group_id,)).fetchone() is None:
                raise KeyError(group_id)
            n = conn.execute("SELECT COALESCE(MAX(n), 0) + 1 FROM client_contracts").fetchone()[0]
            c = ClientContract(id=f"{CLIENT_CONTRACT_PREFIX}{n:02d}", group_id=group_id, legal_entity=legal_entity,
                               insurer_hint=insurer_hint, number=number, created_at=now(),
                               insurer_name=insurer_name, force_generic=force_generic)
            conn.execute("INSERT INTO client_contracts(id, group_id, n, body) VALUES (?,?,?,?)",
                         (c.id, group_id, n, c.model_dump_json()))
        return c

    def _group_contracts(self, group_id: str) -> list[ClientContract]:
        return [ClientContract.model_validate_json(r[0]) for r in
                self._all("SELECT body FROM client_contracts WHERE group_id = ? ORDER BY n", (group_id,))]

    def client_groups(self) -> list[ClientGroup]:
        out = []
        for (body,) in self._all("SELECT body FROM client_groups ORDER BY n"):
            g = ClientGroup.model_validate_json(body)
            g.contracts = self._group_contracts(g.id)
            out.append(g)
        return out

    def client_group(self, group_id: str) -> ClientGroup:
        row = self._one("SELECT body FROM client_groups WHERE id = ?", (group_id,))
        if row is None:
            raise KeyError(group_id)
        g = ClientGroup.model_validate_json(row[0])
        g.contracts = self._group_contracts(g.id)
        return g

    def client_contract(self, contract_id: str) -> ClientContract:
        row = self._one("SELECT body FROM client_contracts WHERE id = ?", (contract_id,))
        if row is None:
            raise KeyError(contract_id)
        return ClientContract.model_validate_json(row[0])

    def save_client_contract(self, c: ClientContract) -> None:
        """Карточка договора клиента целиком (название СК, «не распознавать формат»)."""
        with self._tx() as conn:
            cur = conn.execute("UPDATE client_contracts SET body = ? WHERE id = ?", (c.model_dump_json(), c.id))
            if cur.rowcount == 0:
                raise KeyError(c.id)

    def client_files(self, owner_id: str) -> list[ClientFile]:
        """Файлы договора (owner uNN) или общие файлы группы (owner ugN) в порядке загрузки."""
        return [ClientFile.model_validate_json(r[0]) for r in
                self._all("SELECT body FROM client_files WHERE owner_id = ? ORDER BY ord", (owner_id,))]

    def save_client_file(self, owner_id: str, f: ClientFile) -> None:
        """Запись файла; то же имя у владельца — замена (порядок загрузки сохраняется)."""
        with self._tx() as conn:
            row = conn.execute("SELECT ord FROM client_files WHERE owner_id = ? AND name = ?",
                               (owner_id, f.name)).fetchone()
            ord_ = row[0] if row else conn.execute("SELECT COALESCE(MAX(ord), 0) + 1 FROM client_files").fetchone()[0]
            conn.execute("INSERT OR REPLACE INTO client_files(owner_id, name, ord, body) VALUES (?,?,?,?)",
                         (owner_id, f.name, ord_, f.model_dump_json()))

    def delete_client_file(self, owner_id: str, name: str) -> bool:
        with self._tx() as conn:
            cur = conn.execute("DELETE FROM client_files WHERE owner_id = ? AND name = ?", (owner_id, name))
            return cur.rowcount > 0

    def save_processed(self, contract_id: str, files: list[ClientFile]) -> None:
        """Снимок файлов (договора и общих файлов группы) на момент успешной обработки — заменяет прежний."""
        body = json.dumps([f.model_dump(mode="json") for f in files], ensure_ascii=False)
        with self._tx() as conn:
            conn.execute("INSERT OR REPLACE INTO client_processed(contract_id, body) VALUES (?, ?)",
                         (contract_id, body))

    def processed_files(self, contract_id: str) -> list[ClientFile] | None:
        """Снимок последней успешной обработки; None — договор ещё не обрабатывался успешно."""
        row = self._one("SELECT body FROM client_processed WHERE contract_id = ?", (contract_id,))
        return [ClientFile.model_validate(x) for x in json.loads(row[0])] if row else None

    # ------------------------------------------------------------------ задания обработки договора клиента

    def save_job(self, job: OnboardingJob) -> None:
        with self._tx() as conn:
            row = conn.execute("SELECT ord FROM onboarding_jobs WHERE id = ?", (job.id,)).fetchone()
            ord_ = row[0] if row else \
                conn.execute("SELECT COALESCE(MAX(ord), 0) + 1 FROM onboarding_jobs").fetchone()[0]
            conn.execute("INSERT OR REPLACE INTO onboarding_jobs(id, contract_id, ord, body) VALUES (?,?,?,?)",
                         (job.id, job.contract_id, ord_, job.model_dump_json()))

    def job(self, job_id: str) -> OnboardingJob:
        row = self._one("SELECT body FROM onboarding_jobs WHERE id = ?", (job_id,))
        if row is None:
            raise KeyError(job_id)
        return OnboardingJob.model_validate_json(row[0])

    def last_job(self, contract_id: str) -> OnboardingJob | None:
        row = self._one("SELECT body FROM onboarding_jobs WHERE contract_id = ? ORDER BY ord DESC LIMIT 1",
                        (contract_id,))
        return OnboardingJob.model_validate_json(row[0]) if row else None

    # ------------------------------------------------------------------ разметка таблиц (ADR 0012; Registry)

    @staticmethod
    def _ref_key(ref: TableRef) -> str:
        return json.dumps([ref.doc_id, ref.kind, ref.index, ref.sheet], ensure_ascii=False)

    def table_mappings(self, contract_id: str) -> list[tuple[TableRef, TableMapping]]:
        """Разметки таблиц договора в порядке сохранения."""
        out = []
        for (body,) in self._all("SELECT body FROM table_mappings WHERE contract_id = ? ORDER BY ord",
                                 (contract_id,)):
            d = json.loads(body)
            out.append((TableRef.model_validate(d["ref"]), TableMapping.model_validate(d["mapping"])))
        return out

    def save_table_mapping(self, contract_id: str, ref: TableRef, mapping: TableMapping) -> None:
        """Разметка таблицы договора; та же таблица — замена (порядок сохраняется)."""
        key = self._ref_key(ref)
        body = json.dumps({"ref": ref.model_dump(mode="json"), "mapping": mapping.model_dump(mode="json")},
                          ensure_ascii=False)
        with self._tx() as conn:
            row = conn.execute("SELECT ord FROM table_mappings WHERE contract_id = ? AND ref = ?",
                               (contract_id, key)).fetchone()
            ord_ = row[0] if row else conn.execute("SELECT COALESCE(MAX(ord), 0) + 1 FROM table_mappings").fetchone()[0]
            conn.execute("INSERT OR REPLACE INTO table_mappings(contract_id, ref, ord, body) VALUES (?,?,?,?)",
                         (contract_id, key, ord_, body))

    def delete_table_mapping(self, contract_id: str, ref: TableRef) -> bool:
        with self._tx() as conn:
            cur = conn.execute("DELETE FROM table_mappings WHERE contract_id = ? AND ref = ?",
                               (contract_id, self._ref_key(ref)))
            return cur.rowcount > 0

    def insurer_profiles(self) -> list[InsurerProfile]:
        return [InsurerProfile.model_validate_json(r[0]) for r in
                self._all("SELECT body FROM insurer_profiles ORDER BY n")]

    def insurer_profile(self, profile_id: str) -> InsurerProfile:
        row = self._one("SELECT body FROM insurer_profiles WHERE id = ?", (profile_id,))
        if row is None:
            raise KeyError(profile_id)
        return InsurerProfile.model_validate_json(row[0])

    def save_insurer_profile(self, insurer_name: str, signature: str, mapping: TableMapping) -> InsurerProfile:
        """Профиль СК по подписи заголовка: та же подпись — обновление (одна разметка на подпись, чтобы
        автоприменение было однозначным), иначе новый ipN."""
        with self._tx() as conn:
            row = conn.execute("SELECT body FROM insurer_profiles WHERE signature = ?", (signature,)).fetchone()
            if row is not None:
                p = InsurerProfile.model_validate_json(row[0])
                p = p.model_copy(update={"insurer_name": insurer_name, "mapping": mapping, "updated_at": now()})
                conn.execute("UPDATE insurer_profiles SET body = ? WHERE id = ?", (p.model_dump_json(), p.id))
                return p
            n = conn.execute("SELECT COALESCE(MAX(n), 0) + 1 FROM insurer_profiles").fetchone()[0]
            t = now()
            p = InsurerProfile(id=f"{PROFILE_PREFIX}{n}", insurer_name=insurer_name, signature=signature,
                               mapping=mapping, created_at=t, updated_at=t)
            conn.execute("INSERT INTO insurer_profiles(id, n, signature, body) VALUES (?,?,?,?)",
                         (p.id, n, signature, p.model_dump_json()))
            return p

    def delete_insurer_profile(self, profile_id: str) -> bool:
        with self._tx() as conn:
            conn.execute("DELETE FROM profile_uses WHERE profile_id = ?", (profile_id,))
            return conn.execute("DELETE FROM insurer_profiles WHERE id = ?", (profile_id,)).rowcount > 0

    def profile_used(self, profile_id: str, contract_id: str | None = None) -> None:
        """Счётчик автоприменений профиля: +1 за договор (contract_id — повторная обработка того же договора счётчик
        не меняет); без contract_id — +1 (прежний вызов). Удалённый профиль — без ошибки."""
        with self._tx() as conn:
            row = conn.execute("SELECT body FROM insurer_profiles WHERE id = ?", (profile_id,)).fetchone()
            if row is None:
                return
            if contract_id is not None and conn.execute(
                    "INSERT OR IGNORE INTO profile_uses(profile_id, contract_id) VALUES (?, ?)",
                    (profile_id, contract_id)).rowcount == 0:
                return
            p = InsurerProfile.model_validate_json(row[0])
            p.used += 1
            conn.execute("UPDATE insurer_profiles SET body = ? WHERE id = ?", (p.model_dump_json(), p.id))

    # ------------------------------------------------------------------ вложения чата (ADR 0016)

    def add_attachment(self, chat_id: str, attachment_id: str, sha256: str, body: dict,
                       clauses: list[Clause]) -> None:
        """Вложение чата и его пункты одной транзакцией (body — карточка вложения: документ, куда добавлен)."""
        with self._tx() as conn:
            if conn.execute("SELECT 1 FROM chats WHERE id = ?", (chat_id,)).fetchone() is None:
                raise KeyError(chat_id)
            ord_ = conn.execute("SELECT COALESCE(MAX(ord), 0) + 1 FROM chat_attachments").fetchone()[0]
            conn.execute("INSERT INTO chat_attachments(id, chat_id, ord, sha256, body) VALUES (?,?,?,?,?)",
                         (attachment_id, chat_id, ord_, sha256, json.dumps(body, ensure_ascii=False)))
            conn.executemany("INSERT INTO attachment_clauses(attachment_id, ord, body) VALUES (?,?,?)",
                             [(attachment_id, i, c.model_dump_json()) for i, c in enumerate(clauses)])

    def save_attachment(self, attachment_id: str, body: dict) -> None:
        with self._tx() as conn:
            cur = conn.execute("UPDATE chat_attachments SET body = ? WHERE id = ?",
                               (json.dumps(body, ensure_ascii=False), attachment_id))
            if cur.rowcount == 0:
                raise KeyError(attachment_id)

    def attachments(self, chat_id: str) -> list[dict]:
        """Карточки вложений чата в порядке загрузки (без пунктов)."""
        return [json.loads(r[0]) for r in
                self._all("SELECT body FROM chat_attachments WHERE chat_id = ? ORDER BY ord", (chat_id,))]

    def attachment(self, attachment_id: str) -> tuple[str, dict]:
        """(чат вложения, карточка); KeyError — нет такого."""
        row = self._one("SELECT chat_id, body FROM chat_attachments WHERE id = ?", (attachment_id,))
        if row is None:
            raise KeyError(attachment_id)
        return row[0], json.loads(row[1])

    def attachment_by_sha(self, chat_id: str, sha256: str) -> dict | None:
        """Тот же файл, уже прикреплённый к этому чату (повторная загрузка не плодит копий)."""
        row = self._one("SELECT body FROM chat_attachments WHERE chat_id = ? AND sha256 = ? ORDER BY ord DESC "
                        "LIMIT 1", (chat_id, sha256))
        return json.loads(row[0]) if row else None

    def attachment_clauses(self, attachment_id: str) -> list[Clause]:
        return [Clause.model_validate_json(r[0]) for r in
                self._all("SELECT body FROM attachment_clauses WHERE attachment_id = ? ORDER BY ord",
                          (attachment_id,))]

    def delete_attachment(self, chat_id: str, attachment_id: str) -> dict:
        """Удалить вложение чата (карточку, пункты, отметку «активное»); вложение другого чата — KeyError.
        Возвращает удалённую карточку (по ней сервис удаляет файл)."""
        with self._tx() as conn:
            row = conn.execute("SELECT chat_id, body FROM chat_attachments WHERE id = ?", (attachment_id,)).fetchone()
            if row is None or row[0] != chat_id:
                raise KeyError(attachment_id)
            conn.execute("DELETE FROM chat_active_attachment WHERE chat_id = ? AND attachment_id = ?",
                         (chat_id, attachment_id))
            conn.execute("DELETE FROM attachment_clauses WHERE attachment_id = ?", (attachment_id,))
            conn.execute("DELETE FROM chat_attachments WHERE id = ?", (attachment_id,))
            return json.loads(row[1])

    def delete_chat(self, chat_id: str) -> list[str]:
        """Удалить чат целиком одной транзакцией: обращения, сообщения, черновики, вложения и их пункты, отметку
        «активное вложение». Нет чата — KeyError. Возвращает id удалённых вложений (по ним сервис удаляет файлы)."""
        with self._tx() as conn:
            if conn.execute("SELECT 1 FROM chats WHERE id = ?", (chat_id,)).fetchone() is None:
                raise KeyError(chat_id)
            atts = [r[0] for r in conn.execute("SELECT id FROM chat_attachments WHERE chat_id = ?", (chat_id,))]
            reqs = "SELECT id FROM requests WHERE chat_id = ?"
            conn.execute(f"DELETE FROM drafts WHERE request_id IN ({reqs})", (chat_id,))
            conn.execute(f"DELETE FROM messages WHERE request_id IN ({reqs})", (chat_id,))
            conn.execute("DELETE FROM requests WHERE chat_id = ?", (chat_id,))
            conn.execute("DELETE FROM chat_active_attachment WHERE chat_id = ?", (chat_id,))
            conn.execute("DELETE FROM attachment_clauses WHERE attachment_id IN "
                         "(SELECT id FROM chat_attachments WHERE chat_id = ?)", (chat_id,))
            conn.execute("DELETE FROM chat_attachments WHERE chat_id = ?", (chat_id,))
            conn.execute("DELETE FROM chats WHERE id = ?", (chat_id,))
            return atts

    def active_attachment(self, chat_id: str) -> str | None:
        row = self._one("SELECT attachment_id FROM chat_active_attachment WHERE chat_id = ?", (chat_id,))
        return row[0] if row else None

    def set_active_attachment(self, chat_id: str, attachment_id: str | None) -> None:
        """Активное вложение чата (вопросы отвечаются по нему); None — снова по договору чата. Вложение — только
        этого чата (ADR 0005): чужое — KeyError."""
        with self._tx() as conn:
            if attachment_id is None:
                conn.execute("DELETE FROM chat_active_attachment WHERE chat_id = ?", (chat_id,))
                return
            row = conn.execute("SELECT chat_id FROM chat_attachments WHERE id = ?", (attachment_id,)).fetchone()
            if row is None or row[0] != chat_id:
                raise KeyError(attachment_id)
            conn.execute("INSERT OR REPLACE INTO chat_active_attachment(chat_id, attachment_id) VALUES (?, ?)",
                         (chat_id, attachment_id))

    # ------------------------------------------------------------------ настройки сервиса (ключ → значение)

    def setting(self, key: str, default: object = None) -> object:
        row = self._one("SELECT value FROM app_settings WHERE key = ?", (key,))
        return json.loads(row[0]) if row else default

    def save_setting(self, key: str, value: object) -> None:
        with self._tx() as conn:
            conn.execute("INSERT OR REPLACE INTO app_settings(key, value) VALUES (?, ?)",
                         (key, json.dumps(value, ensure_ascii=False)))
