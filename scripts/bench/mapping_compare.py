"""Ревизия разметки таблиц без синтетики (ADR 0012 п. 5): договор комплекта загружается как договор клиента с
«не распознавать формат» (force_generic), таблицы перечня размечает «оператор» (правила ниже), и результат
сравнивается с разбором адаптера СК по исходному договору.

Оператор (что человек видит на экране разметки и ставит руками) — данных адаптера не видит:
  * таблицы — те, где экран предложил разметку (table_mapping.suggest с числом таблиц той же раскладки, как в API
    GET …/tables); таблица из одной колонки без предложения — перечень, если у большинства строк «N. … Адрес: …»
    (человек видит список клиник), колонка 0 = название;
  * таблица программ без заголовка (Дополнение §10, «Ренессанс»): одна разметка — предложение экрана
    (header_row = None, программа из названия таблицы, роли по содержимому) с «Применить ко всем таблицам с такой
    же раскладкой» (table_mapping.same_signature_tables — то же, что PUT mapping с apply_same_layout);
  * таблица с заголовком: роли — предложение экрана (без ручных ролей); программа — колонка программы, «из названия таблицы / строк «Вариант № N …»» (предложение экрана,
    w9-fix п. 4: перечень одной колонкой с блоками вариантов), иначе программа для всей таблицы, если её видно в
    первых строках; программы не видно — таблица не размечается (PUT требует программу, w9-fix п. 1г).
Профиль СК (--profile, по умолчанию c06 → c07): разметки исходного договора сохраняются профилями по подписи, второй
договор той же СК обрабатывается без разметки — печатается, сколько таблиц размечено автоматически и сколько строк
адаптера (название + адрес, и с программой) нашлось.

Печатается (без текстов документов и названий клиник):
  * строк перечня: адаптер / разметка; строк адаптера, чья пара «название + адрес» нашлась в разметке, и с той же
    программой;
  * ответы ревизий review6 / review7 (вопросы этого договора, без ГК) с выбором менеджера: эталонная строка
    адаптера → строка разметки с тем же названием и адресом; «нет в перечне» → none; строгий кандидат — как в
    review_eval. Сравнение: метка клиники (Checks.clinic_in_program: yes / absent / not_found) и метка ответа;
    ложные «можно» / «нельзя» — метка разметки allowed / denied, а у исходного договора другая.

Матрица «программа × вид помощи» (ADR 0013, --matrix): оператор у каждой размеченной таблицы перечня ставит флажок
«Виды помощи из этого перечня → матрица» (derive_care), а таблицу, которую экран предложил как матрицу (suggest с
программами договора: ≥ 2 колонок заголовка — программы, слева виды помощи), размечает матрицей. Печатается: строк
матрицы адаптер / разметка, пар «программа × вид помощи» совпало (точно и по виду помощи config/care_types.yaml), из
них с тем же present; ответы — метка без выбора клиники и с эталонным выбором (clinic_row_id набора → строка копии с
тем же названием и адресом), совпадение с исходным договором и ложные «можно» / «нельзя» против эталона набора
(expected_label) у копии и у исходного договора.

Длинная матрица «для всех программ» (дополнение CONTRACTS §11, --star-matrix <договор>:<тип документа>:<таблица>:
<колонка вида помощи>:<колонка «входит»>, номера с 0): оператор дополнительно размечает эту таблицу длинной матрицей
с программой «Все программы договора» (TableMapping.program = "*"), строка заголовка — первая (СОГАЗ c10 t7:
«специалист | да» — c10:program:7:0:2).

Оператор «строго по экрану» (--screen, этап w10-fix2; как проверяющий w10-matrix через HTTP API): договоры одной СК —
в порядке аргументов; у каждого: обработка → таблицы с предложением экрана (tm.suggest с профилями, числом таблиц той
же раскладки, СК договора и программами последней обработки — как GET …/tables) → у перечня флажок «Виды помощи из
этого перечня → матрица» → пробный разбор: подозрительная разметка (check_any) не сохраняется → сохранение
(«Применить ко всем таблицам с такой же раскладкой», если строки заголовка нет; у первого договора СК — «Сохранить
как профиль СК»); таблицы, которые профиль СК размечает сам (auto_profile), не трогаются; затем обработка и второй
проход по оставшимся таблицам (программы уже известны) и обработка. Ручных ролей нет (колонка «Телефон» —
только если её предложил экран). Ответы — все наборы data/derived/review/*.jsonl, кроме holdout, вопросы этого
договора без ГК: метка исходного договора и копии (без выбора и с эталонным выбором), совпадение с исходным, ложные
«можно» / «нельзя» против эталона набора (review_eval.expected_for) у копии — отдельно общие с исходным договором.

Пишется только во временную папку (копия kit.sqlite и файлы клиента), общая data/ не меняется.
Запуск из app/: uv run python scripts/bench/mapping_compare.py [c02 c06 c09] [--limit N] [--profile c06:c07]
                 [--matrix] [--diff] [--star-matrix c10:program:7:0:2]
                 uv run python scripts/bench/mapping_compare.py --screen c03 c04 c05 c07 c08 c11 c12 c13 [--limit N]
"""
from __future__ import annotations

import collections
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts" / "bench"))
os.environ.setdefault("DMS_ENGINE_MODE", "guided")

from dms_core import engine, kit, onboarding
from dms_core import table_mapping as tm
from dms_core.config import derived_dir, kit_dir
from dms_core.constants import CLINIC_PICK_NONE
from dms_core.schemas import ChatContext, ClientFile, TableMapping
from dms_core.store import KitStore
from dms_core.tools.clinics import normalize_name, strict_name_match

# ответ по каждому договору (engine._one_contract) — чтобы видеть Checks.clinic_in_program без правки движка
CAPTURE: dict[str, dict] = {}
_orig_one = engine._one_contract


def _one(*a: Any, **k: Any) -> dict:
    res = _orig_one(*a, **k)
    CAPTURE[res["contract_id"]] = res
    return res


engine._one_contract = _one


def put_file(reg: Any, croot: Path, gid: str, cid: str, src: Path, doc_type: str) -> None:
    dst = croot / gid / cid / src.name
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src, dst)
    data = dst.read_bytes()
    reg.save_client_file(cid, ClientFile(name=src.name, doc_type=doc_type, fmt=src.suffix.lstrip(".").lower(),
                                         size=len(data), sha256=hashlib.sha256(data).hexdigest(),
                                         uploaded_at=dt.datetime.now(dt.UTC)))


def load_client(reg: Any, st: Any, croot: Path, source: str, *, legal: str = "Юрлицо ревизии",
                insurer_name: str | None = None) -> Any:
    """Договор комплекта → договор клиента с force_generic (и названием СК в карточке — для автоприменения
    профиля); файлы — копии во временной папке."""
    from dms_core.adapters.base import abs_path

    root = kit_dir()
    kc = next(c for g in kit.scan(root) for c in g.contracts if c.id == source)
    g = reg.create_client_group(f"Ревизия {source}")
    cc = reg.add_client_contract(g.id, legal, force_generic=True, insurer_name=insurer_name)
    for kf in kc.files:
        put_file(reg, croot, g.id, cc.id, abs_path(root, kf), kf.doc_type)
    return cc


def client_tables(reg: Any, st: Any, croot: Path, cc: Any) -> list:
    group = reg.client_group(cc.group_id)
    kc = onboarding.kit_contract(group, cc, reg.client_files(cc.id))
    return tm.list_tables(kc, croot, {d.source_path: d.id for d in st.documents(cc.id)})


_VARIANT = re.compile(r"^\s*Вариант\s*№?\s*\d+\s*[.:–—-]?\s*(.+?)\s*$", re.IGNORECASE)


def _visible_program(rows: list) -> str | None:
    """Программа, которую оператор читает в первых строках таблицы: «Вариант № 1 <программа>»."""
    for row in rows[:tm.HEADER_SCAN]:
        cells = [c for c in row if c.strip()]
        m = _VARIANT.match(cells[0]) if len(cells) == 1 else None
        if m:
            return m.group(1)
    return None


def _numbered_list(rows: list) -> bool:
    """Одноколоночный список клиник (так оператор узнаёт перечень глазами): у большинства строк номер «N.», и у
    большинства нумерованных — «Адрес:» (остальные строки — подзаголовки разделов и условия)."""
    cells = [r[0] for r in rows if r and r[0].strip()]
    num = [c for c in cells if re.match(r"^\s*\d{1,4}[.)]\s", c)]
    addr = sum(1 for c in num if "адрес" in c.casefold())
    return bool(num) and len(num) / len(cells) >= tm.MAJORITY and addr / len(num) >= tm.MAJORITY


def operator_mappings(tables: list, programs: list[str] | None = None, derive: bool = False
                      ) -> list[tuple[Any, TableMapping]]:
    """Разметка, которую поставил бы оператор (правила — в описании модуля; данных адаптера нет). derive — флажок
    «Виды помощи из этого перечня → матрица» у таблиц перечня; programs — программы договора (экран предлагает
    матрицу по ним)."""
    counts = tm.layout_counts(tables)
    done: dict[tuple, TableMapping] = {}
    order: list[Any] = []
    for ref, _title, rows in tables:
        if tm.ref_key(ref) in done:
            continue                       # уже размечена «Применить ко всем таблицам с такой же раскладкой»
        layout = tm.layout_signature(rows)
        m, _ = tm.suggest(rows, [], counts.get(layout, 1) if layout else 1, programs=programs if derive else None)
        if m is not None and m.role == "care_matrix":
            done[tm.ref_key(ref)] = m      # экран предложил матрицу — оператор её принимает
            order.append(ref)
            continue
        if m is None:
            if tm.n_columns(rows) != 1 or not _numbered_list(rows):
                continue
            m = TableMapping(header_row=0, columns={0: "clinic_name"})
        if derive:
            m = m.model_copy(update={"derive_care": True})
        if m.header_row is None:
            # одна разметка + «Применить ко всем таблицам с такой же раскладкой»
            for t in tm.same_signature_tables(tables, rows, m):
                if tm.ref_key(t[0]) not in done:
                    done[tm.ref_key(t[0])] = m
                    order.append(t[0])
            continue
        # роли — только предложение экрана (этап w10-fix2: «по направлению» в колонке «Телефон» экран предлагает сам,
        # по содержимому всей таблицы; ручной роли у оператора нет)
        cols = dict(m.columns)
        # программа: колонка, «из названия / строк-вариантов» (предложение экрана), иначе видимая в таблице
        program = None if "program" in cols.values() or m.program_from_title else _visible_program(rows)
        mm = TableMapping(header_row=m.header_row, columns=cols, program=program,
                          program_from_title=m.program_from_title, derive_care=derive)
        if not tm.has_program_source(mm):
            continue                       # PUT без программы — 422 mapping_no_program: оператор такую не сохранит
        done[tm.ref_key(ref)] = mm
        order.append(ref)
    return [(r, done[tm.ref_key(r)]) for r in order]


def star_matrix(st: Any, cc: Any, tables: list, source: str, argv: list[str]) -> list[tuple[Any, TableMapping]]:
    """--star-matrix: длинная матрица с программой «*» для указанной таблицы договора (см. описание модуля)."""
    if "--star-matrix" not in argv:
        return []
    src, dtype, index, care, present = argv[argv.index("--star-matrix") + 1].split(":")
    if src != source:
        return []
    docs = {d.id for d in st.documents(cc.id) if str(getattr(d.doc_type, "value", d.doc_type)) == dtype}
    ref = next((t[0] for t in tables if t[0].doc_id in docs and t[0].index == int(index)), None)
    if ref is None:
        print(f"  --star-matrix: таблица {dtype} {index} не найдена")
        return []
    return [(ref, TableMapping(role="care_matrix", matrix_layout="long", header_row=0,
                               columns={int(care): "care_type", int(present): "present"}, program=tm.ALL_PROGRAMS))]


def _key(r: Any) -> tuple[str, str]:
    return normalize_name(r.clinic_name), normalize_name(r.address or "")


def compare_rows(st: Any, source: str, cid: str) -> dict:
    """Строки перечня разметки против адаптера: совпадение «название + адрес» и «название + адрес + программа»."""
    a, m = st.clinics(source), st.clinics(cid)
    ka, km = collections.Counter(map(_key, a)), collections.Counter(map(_key, m))
    pa = collections.Counter((*_key(r), r.program) for r in a)
    pm = collections.Counter((*_key(r), r.program) for r in m)
    distinct = set(ka)
    return {"adapter": len(a), "mapped": len(m), "matched_rows": sum(min(n, km[k]) for k, n in ka.items()),
            "matched_with_program": sum(min(n, pm[k]) for k, n in pa.items()),
            "distinct_adapter": len(distinct), "distinct_found": len(distinct & set(km))}


_CARE_CODES: list[tuple[str, list[str]]] | None = None


def care_code(text: str) -> str:
    """Вид помощи → код config/care_types.yaml (первая фраза названия / синонима словами внутри текста), иначе
    нормализованный текст: «Стационарная помощь (плановая госпитализация)» ↔ «Стационарная помощь» ↔ «inpatient»."""
    global _CARE_CODES
    if _CARE_CODES is None:
        from dms_core.config import load_yaml

        types = (load_yaml("care_types.yaml") or {}).get("care_types") or {}
        _CARE_CODES = [(code, [tm.norm_cell(str(x)) for x in [spec.get("name", ""), *(spec.get("synonyms") or [])]
                               if tm.norm_cell(str(x))]) for code, spec in types.items()]
    n = tm.norm_cell(text)
    for code, phrases in _CARE_CODES:
        if any(f" {p} " in f" {n} " for p in phrases):
            return code
    return n


def compare_care(st: Any, source: str, cid: str) -> dict:
    """Матрица разметки против адаптера: строк; пар «программа × вид помощи» (программа и вид помощи после
    нормализации) — совпало точно и по коду вида помощи; из совпавших по коду — с тем же present."""
    def pairs(rows: list, code: bool) -> dict[tuple[str, str], set[bool]]:
        out: dict[tuple[str, str], set[bool]] = {}
        for m in rows:
            for part in m.care_type.split("|"):
                key = (tm.norm_cell(m.program), care_code(part) if code else tm.norm_cell(part))
                out.setdefault(key, set()).add(m.present)
        return out

    a, m = st.care(source), st.care(cid)
    ea, em = pairs(a, False), pairs(m, False)
    ca, cm = pairs(a, True), pairs(m, True)
    same = [k for k in ca if k in cm]
    return {"adapter": len(a), "mapped": len(m), "mapped_present": sum(x.present for x in m),
            "adapter_absent": sum(not x.present for x in a), "pairs_adapter": len(ea), "pairs_exact": len(set(ea) & set(em)),
            "pairs_code_adapter": len(ca), "pairs_code_mapped": len(cm), "pairs_code": len(same),
            "present_equal": sum(1 for k in same if ca[k] == cm[k]),
            "mapped_only_code": len(set(cm) - set(ca))}


def save_profiles(reg: Any, tables: list, maps: list, insurer: str) -> int:
    """Профили СК по разметкам договора (флажок «Сохранить как профиль СК»): одна разметка на подпись."""
    rows_of = {tm.ref_key(t[0]): t[2] for t in tables}
    sigs = set()
    for ref, m in maps:
        sig = tm.mapping_signature(rows_of[tm.ref_key(ref)], m)
        if sig and sig not in sigs:
            reg.save_insurer_profile(insurer, sig, m)
            sigs.add(sig)
    return len(sigs)


def _questions(source: str) -> list[dict]:
    rows: list[dict] = []
    for name in ("review6.jsonl", "review7.jsonl"):
        p = derived_dir() / "review" / name
        if p.is_file():
            rows += [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines() if x.strip()]
    return [r for r in rows if r.get("c") == source and not r.get("g")]


def _clinic_check(cid: str) -> str:
    res = CAPTURE.get(cid)
    ch = res["verified"].draft.checks if res else None
    return getattr(ch, "clinic_in_program", None) or "-"


def _checks(cid: str) -> str:
    """Проверки SC 01 ответа (без текстов документов): для --checks."""
    res = CAPTURE.get(cid)
    ch = res["verified"].draft.checks if res else None
    return ch.model_dump_json(exclude_none=True) if ch is not None else "-"


def _ask(st: Any, q: str, cid: str, program: str | None, pick: str | None) -> tuple[str, str, Any]:
    CAPTURE.pop(cid, None)
    a = engine.answer(q, ChatContext(contract_id=cid, program=program), llm=None, store=st,
                      clinic_picks={cid: pick} if pick else None)
    return (a.label.value if a.label else "-"), _clinic_check(cid), a


def map_pick(st: Any, source: str, cid: str, program: str | None, pick: str, choice: Any) -> str | None:
    """Выбор менеджера в исходном договоре → выбор в размеченном: строка с тем же названием и адресом (лучше — тот
    же вид помощи), none → none."""
    if pick == CLINIC_PICK_NONE:
        return CLINIC_PICK_NONE
    src = next((r for r in st.clinics(source) if r.id == pick), None)
    if src is None:
        return None
    rows = [r for r in st.clinics(cid, program=program) if _key(r) == _key(src)] if program else []
    rows.sort(key=lambda r: r.care_type != src.care_type)
    return rows[0].id if rows else None


def compare_answers(st: Any, source: str, cid: str, limit: int | None) -> dict:
    from review_eval import simulated_picks

    progs = {p.name for p in st.contract(cid).programs}
    t = collections.Counter()
    for r in _questions(source)[:limit]:
        p = r.get("p")
        gold = r.get("expected_label")
        l1, k1, a1 = _ask(st, r["q"], source, p, None)
        l1n = l1
        picks = simulated_picks(r, a1.clinic_choices) if a1.clinic_choices else {}
        pick = picks.get(source, (None, ""))[0]
        if pick:
            l1, k1, _ = _ask(st, r["q"], source, p, pick)
        pm = p if p in progs else None
        l2, k2, a2 = _ask(st, r["q"], cid, pm, None)
        l2n = l2
        t["label_equal_nopick"] += l1n == l2n
        if l2n == "allowed" and gold != "allowed":
            t["false_allowed_gold_nopick"] += 1
            print(f"  ложное «можно» против эталона (без выбора) {r['id']}: эталон {gold}, копия {l2n}")
        if l2n == "denied" and gold != "denied":
            t["false_denied_gold_nopick"] += 1
            print(f"  ложное «нельзя» против эталона (без выбора) {r['id']}: эталон {gold}, копия {l2n}")
        pick2 = None
        if pick and a2.clinic_choices:
            how = picks[source][1]
            if how == "strict":
                ch = a2.clinic_choices[0]
                pick2 = next((x.id for x in ch.candidates if strict_name_match(ch.asked, x.clinic_name,
                                                                                x.legal_name)), None)
            else:
                pick2 = map_pick(st, source, cid, pm, pick, a2.clinic_choices[0])
        if pick2:
            l2, k2, _ = _ask(st, r["q"], cid, pm, pick2)
        t["questions"] += 1
        t["program_known"] += pm is not None
        t["picked_src"] += bool(pick)
        t["picked_map"] += bool(pick2)
        t["clinic_equal"] += k1 == k2
        t["clinic_equal_picked"] += bool(pick) and k1 == k2      # метка клиники при эталонном выборе
        t["label_equal"] += l1 == l2
        t["label_equal_picked"] += bool(pick) and l1 == l2
        t[f"label_src_{l1}"] += 1
        t[f"label_map_{l2}"] += 1
        if gold:
            t["gold_equal_src"] += l1 == gold
            t["gold_equal_map"] += l2 == gold
            if l2 == "allowed" and gold != "allowed":
                t["false_allowed_gold"] += 1
                print(f"  ложное «можно» против эталона {r['id']}: эталон {gold}, исходный {l1}, копия {l2}")
            if l2 == "denied" and gold != "denied":
                t["false_denied_gold"] += 1
                print(f"  ложное «нельзя» против эталона {r['id']}: эталон {gold}, исходный {l1}, копия {l2}")
            t["false_allowed_gold_src"] += l1 == "allowed" and gold != "allowed"
            t["false_denied_gold_src"] += l1 == "denied" and gold != "denied"
        t[f"clinic_src_{k1}"] += 1
        t[f"clinic_map_{k2}"] += 1
        if l2 == "allowed" and l1 != "allowed":
            t["false_allowed"] += 1
            print(f"  ложное «можно» {r['id']}: исходный {l1}/{k1}, разметка {l2}/{k2}")
        if l2 == "denied" and l1 != "denied":
            t["false_denied"] += 1
            print(f"  ложное «нельзя» {r['id']}: исходный {l1}/{k1}, разметка {l2}/{k2}")
        if l2 == "allowed" and "--diff" in sys.argv:
            print(f"  «можно» копии {r['id']}: исходный {l1}, эталон {gold}")
        if l2 != l1 and "--diff" in sys.argv:
            print(f"  метка {r['id']}: исходный {l1}/{k1}, разметка {l2}/{k2}")
            if "--checks" in sys.argv:
                _ask(st, r["q"], source, p, pick)
                print("    исходный:", _checks(source))
                _ask(st, r["q"], cid, pm, pick2)
                print("    копия:   ", _checks(cid))
        if k1 != k2 and "--diff" in sys.argv:
            print(f"  клиника {r['id']}: исходный {l1}/{k1}, разметка {l2}/{k2}, выбор {bool(pick)}/{bool(pick2)}")
    for k in ("false_allowed", "false_denied", "false_allowed_gold", "false_denied_gold", "false_allowed_gold_nopick",
              "false_denied_gold_nopick"):
        t.setdefault(k, 0)
    return dict(t)


# ---------------------------------------------------------------- оператор строго по экрану (--screen)

def _store_programs(st: Any, cid: str) -> list[str]:
    """Программы последней обработки и программы строк перечня (как service._programs)."""
    try:
        names = [p.name for p in st.contract(cid).programs] + [r.program for r in st.clinics(cid)]
    except (KeyError, ValueError):
        return []
    return [p for p in dict.fromkeys(names) if p]


def screen_pass(reg: Any, st: Any, croot: Path, cc: Any, save_profile: bool) -> dict[str, int]:
    """Один проход оператора по экрану разметки (правила — в описании модуля, --screen). Счётчики действий."""
    tables = client_tables(reg, st, croot, cc)
    own = {tm.ref_key(r) for r, _m in reg.table_mappings(cc.id)}
    profiles = list(reg.insurer_profiles())
    programs = _store_programs(st, cc.id)
    counts = tm.layout_counts(tables)
    t = collections.Counter()
    for ref, _title, rows in tables:
        if tm.ref_key(ref) in own:
            continue
        if tm.auto_profile(rows, profiles, cc.insurer_name, programs)[0] is not None:
            t["auto_profile"] += 1
            continue                       # «размечено профилем СК» — оператор не трогает
        layout = tm.layout_signature(rows)
        m, _pid = tm.suggest(rows, profiles, counts.get(layout, 1) if layout else 1, cc.insurer_name, programs)
        if m is None:
            continue
        if m.role != "care_matrix":
            m = m.model_copy(update={"derive_care": True})
        try:
            tm.validate_any(rows, m, single_program=len(programs) == 1)
        except tm.MappingError:
            t["invalid"] += 1
            continue
        if m.role != "care_matrix" and not tm.has_program_source(m):
            t["no_program"] += 1
            continue                       # PUT — 422 mapping_no_program
        if tm.check_any(rows, m, programs):
            t["declined_suspicious"] += 1
            continue                       # пробный разбор предупредил — оператор не сохраняет
        targets = [ref]
        if m.header_row is None and counts.get(layout, 1) > 1:
            targets += [x[0] for x in tm.same_signature_tables(tables, rows, m) if tm.ref_key(x[0]) != tm.ref_key(ref)]
        for r in targets:
            if tm.ref_key(r) not in own:
                reg.save_table_mapping(cc.id, r, m)
                own.add(tm.ref_key(r))
        if save_profile and cc.insurer_name:
            sig = tm.mapping_signature(rows, m)
            if sig:
                reg.save_insurer_profile(cc.insurer_name, sig, tm.profile_mapping(rows, m))
                profiles = list(reg.insurer_profiles())
        t["matrix" if m.role == "care_matrix" else "list"] += 1
        t["tables"] += len(targets)
    return dict(t)


def _all_questions(source: str) -> list[tuple[str, dict]]:
    out: list[tuple[str, dict]] = []
    for p in sorted((derived_dir() / "review").glob("*.jsonl")):
        if "holdout" in p.name:
            continue
        for line in p.read_text(encoding="utf-8").splitlines():
            if line.strip():
                r = json.loads(line)
                if r.get("c") == source and not r.get("g"):
                    out.append((p.name, r))
    return out


def _false(label: str, want: set[str], unknown: bool) -> str | None:
    if not want or unknown:
        return None
    if label == "allowed" and "allowed" not in want:
        return "FA"
    if label == "denied" and "denied" not in want:
        return "FD"
    return None


def compare_gold(st: Any, source: str, cid: str, limit: int | None) -> dict:
    """Метки копии против исходного договора и эталона наборов (описание модуля, --screen)."""
    from review_eval import expected_for, simulated_picks

    progs = {p.name for p in st.contract(cid).programs}
    t = collections.Counter()
    for name, r in _all_questions(source)[:limit]:
        want = expected_for(r)
        if isinstance(want, dict):
            want = want.get(source, (set(), True))
        w, unk = want
        p = r.get("p")
        l1n, _k, a1 = _ask(st, r["q"], source, p, None)
        picks = simulated_picks(r, a1.clinic_choices) if a1.clinic_choices else {}
        pick, how = picks.get(source, (None, ""))
        l1 = _ask(st, r["q"], source, p, pick)[0] if pick else l1n
        pm = p if p in progs else None
        l2n, _k2, a2 = _ask(st, r["q"], cid, pm, None)
        pick2 = None
        if pick and a2.clinic_choices:
            if how == "strict":
                ch = a2.clinic_choices[0]
                pick2 = next((x.id for x in ch.candidates if strict_name_match(ch.asked, x.clinic_name,
                                                                                x.legal_name)), None)
            else:
                pick2 = map_pick(st, source, cid, pm, pick, a2.clinic_choices[0])
        l2 = _ask(st, r["q"], cid, pm, pick2)[0] if pick2 else l2n
        t["questions"] += 1
        t["picked"] += bool(pick)
        t["label_equal"] += l1 == l2
        t["label_equal_nopick"] += l1n == l2n
        if w and not unk:
            t["scored"] += 1
            t["gold_src"] += l1 in w
            t["gold_copy"] += l2 in w
        for tag, lab, src_lab in (("copy", l2, l1), ("copy_nopick", l2n, l1n)):
            fl = _false(lab, w, unk)
            if fl is None:
                continue
            common = _false(src_lab, w, unk) == fl
            t[f"{fl}_{tag}{'_common' if common else ''}"] += 1
            print(f"  {'общее с исходным' if common else 'ЛОЖНОЕ'} {fl} {tag} {name} {r.get('id')}: эталон "
                  f"{sorted(w)}, исходный {src_lab}, копия {lab}")
        for tag, lab in (("src", l1), ("src_nopick", l1n)):
            fl = _false(lab, w, unk)
            if fl:
                t[f"{fl}_{tag}"] += 1
        if l1 != l2 and "--diff" in sys.argv:
            print(f"  метка {r.get('id')}: исходный {l1}, копия {l2}")
    for k in ("FA_copy", "FD_copy", "FA_copy_nopick", "FD_copy_nopick"):
        t.setdefault(k, 0)
    return dict(t)


def main_screen(argv: list[str]) -> int:
    """--screen: договоры по СК в порядке аргументов, профиль первого договора СК → следующие."""
    from dms_api.store import AppStore

    args = [a for a in argv if not a.startswith("--")]
    limit = None
    if "--limit" in argv:
        limit = int(argv[argv.index("--limit") + 1])
        args = [a for a in args if a != str(limit)]
    tmp = Path(tempfile.mkdtemp(prefix="dms-screen-"))
    (tmp / "derived").mkdir(parents=True)
    shutil.copyfile(derived_dir() / "kit.sqlite", tmp / "derived" / "kit.sqlite")
    st = KitStore(tmp / "derived" / "kit.sqlite")
    reg = AppStore(tmp / "app" / "app.sqlite")
    croot = tmp / "derived" / "clients"
    first: dict[str, str] = {}
    total = collections.Counter()
    for source in args:
        insurer = str(getattr(st.contract(source).insurer, "value", st.contract(source).insurer))
        is_first = insurer not in first
        first.setdefault(insurer, source)
        cc = load_client(reg, st, croot, source, insurer_name=f"СК {insurer}")
        rep0 = onboarding.process(cc.id, store=st, registry=reg, root=croot, run_selfcheck=False)
        acts = screen_pass(reg, st, croot, cc, save_profile=is_first)
        onboarding.process(cc.id, store=st, registry=reg, root=croot, run_selfcheck=False)
        acts2 = screen_pass(reg, st, croot, cc, save_profile=is_first)
        rep = onboarding.process(cc.id, store=st, registry=reg, root=croot, run_selfcheck=False)
        print(f"{source} → {cc.id} (СК {insurer}, {'первый' if is_first else 'профиль ' + first[insurer]}): "
              f"readiness {rep0.readiness}→{rep.readiness}, проход 1 {acts}, проход 2 {acts2}, "
              f"counts {rep.counts}")
        print("  строки:", compare_rows(st, source, cc.id))
        print("  пары матрицы:", compare_care(st, source, cc.id))
        res = compare_gold(st, source, cc.id, limit)
        print("  ответы:", res)
        total.update({k: v for k, v in res.items() if k.startswith(("F", "questions", "scored", "gold", "label"))})
    print("ИТОГО:", dict(total))
    shutil.rmtree(tmp, ignore_errors=True)
    return 0


def main(argv: list[str]) -> int:
    if "--screen" in argv:
        return main_screen(argv)
    args = [a for a in argv if not a.startswith("--")]
    if "--star-matrix" in argv:
        args.remove(argv[argv.index("--star-matrix") + 1])
    limit = None
    if "--limit" in argv:
        limit = int(argv[argv.index("--limit") + 1])
        args = [a for a in args if a != str(limit)]
    sources = args or ["c02", "c06", "c09"]
    prof_pair = ("c06", "c07")
    if "--profile" in argv:
        v = argv[argv.index("--profile") + 1]
        args = [a for a in args if a != v]
        sources = args or sources
        prof_pair = tuple(v.split(":", 1))  # type: ignore[assignment]
    from dms_api.store import AppStore

    tmp = Path(tempfile.mkdtemp(prefix="dms-map-"))
    (tmp / "derived").mkdir(parents=True)
    shutil.copyfile(derived_dir() / "kit.sqlite", tmp / "derived" / "kit.sqlite")    # копия: общая база не трогается
    st = KitStore(tmp / "derived" / "kit.sqlite")
    reg = AppStore(tmp / "app" / "app.sqlite")
    croot = tmp / "derived" / "clients"
    for source in sources:
        cc = load_client(reg, st, croot, source)
        rep0 = onboarding.process(cc.id, store=st, registry=reg, root=croot, run_selfcheck=False)
        tables = client_tables(reg, st, croot, cc)
        programs = [x.name for x in st.contract(cc.id).programs] if rep0.readiness != "failed" else []
        maps = operator_mappings(tables, programs, derive="--matrix" in argv)
        maps += star_matrix(st, cc, tables, source, argv)
        rows_of = {tm.ref_key(t[0]): t[2] for t in tables}
        susp = sum(bool(tm.content_check(rows_of[tm.ref_key(r)], m)) for r, m in maps)
        print(f"  разметок оператора {len(maps)}, подозрительных по содержимому {susp}")
        for ref, m in maps:
            reg.save_table_mapping(cc.id, ref, m)
        rep = onboarding.process(cc.id, store=st, registry=reg, root=croot, run_selfcheck=False)
        n_layout = len({tm.mapping_signature(t[2], m) for t in tables for r, m in maps
                        if tm.ref_key(r) == tm.ref_key(t[0]) and m.header_row is None})
        print(f"{source} → {cc.id}: adapter={rep.adapter} readiness {rep0.readiness}→{rep.readiness} "
              f"таблиц размечено {rep.counts.get('mapped_tables')} (разметок по раскладке {n_layout}) "
              f"программ {rep.counts.get('programs')}")
        print("  строки:", compare_rows(st, source, cc.id))
        if "--matrix" in argv:
            print(f"  матрица: readiness {rep.readiness}, care_rows {rep.counts.get('care_rows')}, "
                  f"mapped_matrices {rep.counts.get('mapped_matrices')}, матриц-таблиц "
                  f"{sum(m.role == 'care_matrix' for _r, m in maps)}")
            print("  пары матрицы:", compare_care(st, source, cc.id))
        print("  ответы:", compare_answers(st, source, cc.id, limit))
        if source == prof_pair[0] and len(prof_pair) == 2:
            n_prof = save_profiles(reg, tables, maps, f"Профиль {source}")
            cc2 = load_client(reg, st, croot, prof_pair[1], legal="Юрлицо профиля", insurer_name=f"Профиль {source}")
            rep2 = onboarding.process(cc2.id, store=st, registry=reg, root=croot, run_selfcheck=False)
            print(f"  профиль {source} → {prof_pair[1]} ({cc2.id}): профилей {n_prof}, таблиц размечено "
                  f"автоматически {rep2.counts.get('mapped_tables')}, программ {rep2.counts.get('programs')}")
            print("  строки профиля:", compare_rows(st, prof_pair[1], cc2.id))
    shutil.rmtree(tmp, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
