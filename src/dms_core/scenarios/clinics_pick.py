"""Подбор альтернативной клиники (ТЗ п. 5.2.2, допущение A6 плана §2).

Кандидаты — строки перечня клиник ТОГО ЖЕ договора и ТОЙ ЖЕ программы с нужным видом помощи (ADR 0005: клиника из
другой программы или другого договора доступной не считается). Условия доступа (франшиза, «по направлению»,
примечание о зоне/порядке) не фильтруют, а называются в основаниях.
Ранжирование A6: без франшизы → в том же городе/районе (`near`) → та же сеть (юрлицо отказавшей клиники) →
порядок перечня. До k штук, у каждой — основания и источник (документ и строка таблицы).
"""
from __future__ import annotations

from ..constants import FRANCHISE_NOT_APPLICABLE
from ..schemas import Alternative, ClinicRow
from ..store.db import KitStore
from ..tools._common import resolve
from ..tools.clinics import _care_matches, _care_phrase_sets, _text_contains, name_score, normalize_name

NEAR_THRESHOLD = 85     # как ADDRESS_THRESHOLD инструмента clinics_query
SAME_CLINIC = 95        # name_score, начиная с которого строка — сама отказавшая клиника


def _no_franchise(r: ClinicRow) -> int:
    """0 — франшизы нет («—» или 0 %), 1 — размер не указан, 2 — франшиза есть (ранжирование A6, первый ключ)."""
    if r.franchise_pct == FRANCHISE_NOT_APPLICABLE or r.franchise_pct == 0:
        return 0
    if r.franchise_pct is None:
        return 1
    return 2


def _is_near(r: ClinicRow, near: str | None) -> bool:
    if not near:
        return False
    return _text_contains(near, r.city, NEAR_THRESHOLD) or _text_contains(near, r.address, NEAR_THRESHOLD)


def _same_network(r: ClinicRow, network: str | None) -> bool:
    if not network:
        return False
    n = normalize_name(network)
    return bool(n) and n in (normalize_name(r.legal_name or ""), normalize_name(r.clinic_name))


def _reason(r: ClinicRow, program: str, near: str | None, same_net: bool) -> str:
    """Основания выбора — только из полей строки перечня (ничего «как обычно бывает»)."""
    parts = [f"та же программа «{program}»", f"вид помощи «{r.care_type}»"]
    if r.franchise_pct == FRANCHISE_NOT_APPLICABLE:
        parts.append("франшиза не применяется")
    elif r.franchise_pct is None:
        parts.append("размер франшизы в перечне не указан")
    else:
        parts.append(f"франшиза {r.franchise_pct:g} %")
    if r.by_referral:
        parts.append("доступ по направлению")
    if r.access_note:
        parts.append(f"условие доступа: {r.access_note}")
    if near and _is_near(r, near):
        parts.append(f"рядом: {near}")
    if same_net:
        parts.append("та же сеть")
    parts.append(f"источник: {r.doc_id}, строка {r.source_ref}")
    return "; ".join(parts)


def pick(contract_id: str, program: str, care_type: str, near: str | None = None, k: int = 5,
         store: KitStore | None = None, *, exclude: str | None = None, network: str | None = None) -> list[Alternative]:
    """Альтернативы по A6. exclude — название отказавшей клиники (её строки не предлагаются);
    network — сеть/юрлицо для ключа «та же сеть» (по умолчанию — юрлицо строк отказавшей клиники)."""
    cid, st = resolve(contract_id, store)
    if not program or not care_type:
        return []                     # без программы и вида помощи альтернатива не определена (ТЗ п. 5.2.2)
    phrases = _care_phrase_sets(care_type)
    rows = [r for r in st.clinics(cid, program=program) if _care_matches(care_type, r.care_type, phrases)]
    if exclude:
        refused = [r for r in rows if name_score(exclude, r) >= SAME_CLINIC]
        if network is None and refused:
            network = refused[0].legal_name
        rows = [r for r in rows if r not in refused]
    seen: set[tuple[str, str]] = set()
    uniq: list[tuple[int, ClinicRow]] = []
    for i, r in enumerate(rows):
        key = (normalize_name(r.clinic_name), (r.address or "").casefold())
        if key in seen:
            continue
        seen.add(key)
        uniq.append((i, r))
    uniq.sort(key=lambda ir: (_no_franchise(ir[1]), not _is_near(ir[1], near), not _same_network(ir[1], network),
                              ir[0]))
    return [Alternative(clinic=r, reason=_reason(r, program, near, _same_network(r, network)))
            for _, r in uniq[:k]]
