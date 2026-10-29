"""Перечисления контрактов (docs/CONTRACTS.md, план §5). Значения — строки, они же уходят в JSON и в конфиги."""
from __future__ import annotations

import os
from enum import StrEnum

ENGINE_VERSION = "0.1.0"


class DocType(StrEnum):
    """Тип документа. Порядок старшинства задаёт config/precedence.yaml, не этот список."""
    contract = "contract"            # договор
    amendment = "amendment"          # дополнительное соглашение
    program = "program"              # программа ДМС (объём помощи, исключения)
    clinics = "clinics"              # перечень / список медицинских организаций
    memo = "memo"                    # памятка застрахованному
    rules = "rules"                  # общие правила страхования СК
    catalog = "catalog"              # каталог страховщика (не часть договора, напр. «ЛПУ_Регионы Стандарт.xlsx»)
    insurer_reply = "insurer_reply"  # ответ СК, вставленный менеджером
    other = "other"


class DocStatus(StrEnum):
    """Статусы готовности документа (ТЗ п. 7.1)."""
    received = "received"      # получен
    extracted = "extracted"    # текст извлечён
    checked = "checked"        # полнота проверена
    indexed = "indexed"        # индексирован
    ready = "ready"            # готов
    error = "error"


class Insurer(StrEnum):
    zetta = "zetta"
    renaissance = "renaissance"
    sogaz = "sogaz"
    unknown = "unknown"


class Scenario(StrEnum):
    sc01 = "sc01"                        # разбор отказа клиники
    sc02_info = "sc02_info"              # условия договора / программы / услуг
    sc02_clinic = "sc02_clinic"          # подбор клиники
    sc02_add_clinic = "sc02_add_clinic"  # добавление клиники (автомат ТЗ п. 5.2.2)
    general = "general"


class Label(StrEnum):
    """Метки SC 01 (ТЗ п. 5.1)."""
    allowed = "allowed"            # можно (отказ необоснован)
    denied = "denied"              # нельзя (отказ обоснован)
    conditional = "conditional"    # можно при условиях
    insufficient = "insufficient"  # недостаточно данных


class Confidence(StrEnum):
    """Статусы уверенности ответа (ТЗ п. 8.1). Процентов уверенности нет."""
    confirmed = "confirmed"                    # есть подтверждение в документах
    need_clarification = "need_clarification"  # нужно уточнение
    contradictory = "contradictory"            # источники противоречат друг другу
    insufficient = "insufficient"              # данных недостаточно


class StatementKind(StrEnum):
    fact = "fact"
    condition = "condition"
    limitation = "limitation"
    comparison = "comparison"
    inference = "inference"


class AnswerMode(StrEnum):
    live = "live"
    demo_saved = "demo_saved"    # сохранённый ответ демо-кейса (нет ключа модели)
    extractive = "extractive"    # без модели: найденные пункты и строки клиник без вывода
    guided = "guided"            # без модели: вывод и метку строит код по документам (ADR 0009)


class RequestStatus(StrEnum):
    open = "open"
    closed = "closed"


class AddClinicState(StrEnum):
    """Состояния автомата добавления клиники (ADR 0004, ТЗ п. 5.2.2)."""
    alt_found = "alt_found"
    not_in_program = "not_in_program"
    hr_wants_add = "hr_wants_add"
    awaiting_insurer = "awaiting_insurer"
    insurer_free = "insurer_free"
    insurer_paid = "insurer_paid"
    hr_agreed = "hr_agreed"
    insurer_confirmed = "insurer_confirmed"   # СК подтвердила добавление после согласия HR (ТЗ п. 5.2.2)
    hr_declined = "hr_declined"
    closed = "closed"
    insufficient = "insufficient"


class DraftTo(StrEnum):
    hr = "hr"
    insurer = "insurer"


# Франшиза в строке клиники: None — в документе не указана; NOT_APPLICABLE — «—» (не применяется), это не 0 %.
FRANCHISE_NOT_APPLICABLE = "—"

# Подтверждение клиники менеджером (ADR 0010): кандидатов в ответе не больше; выбор «нет в перечне»
CLINIC_CHOICE_MAX = 6
CLINIC_PICK_NONE = "none"
CLINIC_SEARCH_PAGE = 20      # строк перечня за один запрос поиска (GET …/clinic-candidates)

# Подключение клиента (ADR 0011)
CLIENT_CONTRACT_PREFIX = "u"       # uNN — договоры клиентов; cNN — комплект организаторов
CLIENT_GROUP_PREFIX = "ug"
CLIENT_MAX_FILES = 40              # файлов на договор
SELFCHECK_MAX = 30                 # вопросов самопроверки на договор

# Разметка таблиц оператором (ADR 0012)
TABLE_PREVIEW_ROWS = 15            # строк таблицы в предпросмотре
MAPPING_SAMPLE_ROWS = 10           # строк перечня в пробном разборе по разметке
PROFILE_PREFIX = "ip"
# Разметка матрицы (ADR 0013): «нельзя» по матрице — только явное «нет»; прочий текст ячейки — пропуск с предупреждением
MATRIX_SAMPLE_ROWS = 20

# Ответ в чате (ADR 0014)
REPLY_POINTS_MAX = 6
# Кэш пунктов и поисковых индексов договоров в памяти (store.KitStore.clauses, index.search.get_index): предел по
# числу пунктов, сверх него вытесняются давно не нужные договоры (круг 2 ревизии w16; комплект — ~60 тыс. пунктов,
# ~260 МБ с индексами; сервер — 2–4 ГБ). 0 — без кэша. Окружение DMS_CLAUSE_CACHE_MAX — для сервера с запасом
# памяти (ГК из 100 договоров, ~640 тыс. пунктов: ~1,2 ГБ и 2–4 с на ответ вместо ~0,5 ГБ и ~20 с).
_cache_env = (os.environ.get("DMS_CLAUSE_CACHE_MAX") or "").strip().replace("_", "")
CLAUSE_CACHE_MAX = int(_cache_env) if _cache_env.isdigit() else 200_000   # опечатка — значение по умолчанию
CLARIFY_OPTIONS_MAX = 24        # вариантов ответа кнопками на уточняющий вопрос (программы; ГК 1 — 16), больше — без кнопок
REPLY_QUOTE_CHARS = 220
