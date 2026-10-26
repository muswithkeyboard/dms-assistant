"""Регрессия меток SC 01 на наборе вопросов ревизии (без модели): метка движка против ожидания проверяющего.

Набор — JSONL с полями id, c (договор), p (программа), q (вопрос), exp (ожидание: «denied 4.1.20»,
«cond/denied …», «allowed/cond», «refusal», «?»). По умолчанию — data/derived/review/review_w5_questions.jsonl
(вне git: производное от документов организаторов). Печатаются только id, метки и ожидание — без текстов ответов.

Счёт:
  ложное «можно»  — метка allowed, а в ожидании «можно» нет (ожидание с метками);
  ложное «нельзя» — метка denied, а в ожидании «нельзя» нет (ожидание с метками);
  совпало         — метка среди меток ожидания.
Ожидания без меток («refusal», «info» — только сценарий; «?» — проверяющий не знает ответа) не считаются ни в
совпадениях, ни в ложных: метка по ним печатается отдельной строкой («без метки в ожидании»), чтобы её можно было
сверить вручную (w5-recall: «refusal» k-06 и «denied (refusal)» d-12 — одна услуга одного договора).

--pick (ADR 0010): для вопросов, где ответ предложил выбор клиники (Answer.clinic_choices), выбор менеджера
моделируется и ответ пересчитывается с engine.answer(..., clinic_picks). Выбор по договору:
  1) поле записи clinic_row_id (id строки перечня; для ГК — словарь {договор: id}) или clinic_pick: "none";
  2) иначе признак «нет в перечне» в разметке проверяющего: класс clin_fake / clin_oprog (выдуманная клиника, клиника
     другой программы) или «нет в …» в ожидании (кроме «нет в матрице» — это вид помощи) → "none";
  3) иначе — первый кандидат, строго совпавший по названию (tools/clinics.strict_name_match); строгого нет — без
     выбора (менеджер бы уточнил). Печатаются отдельно ложные «можно»/«нельзя» без выбора и с выбором, для ложных
     с выбором — правило метки, выбор (строка / none) и способ выбора; названия клиник — только с --names.

Эталонная строка (поле clinic_row_id; ГК — по договорам): печатается, во скольких парах «вопрос × договор» ответ
предложил выбор клиники, у скольких из них эталонная строка среди кандидатов и в каких парах выбора нет (этап w7).
Контекст ГК — поле g (group_id).

Ревизия 7: поле expected_label — метка (строка) или по договорам ГК ({договор: метка}); для ГК счёт ведётся по
парам «вопрос:договор» с меткой Answer.per_contract. Поле no_clinic: true — в вопросе клиники нет; печатается,
в скольких таких вопросах ответ предложил выбор клиники («ложный слот»).

Запуск из app/: uv run python scripts/bench/review_eval.py [путь.jsonl] [--all] [--pick] [--names]
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
os.environ.setdefault("DMS_ENGINE_MODE", "guided")

from dms_core.config import derived_dir
from dms_core.schemas import ChatContext

# слова ожидания → метки (порядок: «not allowed» раньше «allowed»)
_EXP = ((r"not\s+allowed|not\s+in\s+program", "denied"), (r"\bdenied", "denied"), (r"\ballowed", "allowed"),
        (r"\bcond", "conditional"), (r"contradiction", "conditional"), (r"\binsuff", "insufficient"))
# разметка проверяющего «клиники нет в перечне программы»: классы набора и слова ожидания («нет в Прил.4», «нет в
# перечне», «нет в Бизнес»); «нет в матрице» — про вид помощи, не про клинику
_NONE_CLS = frozenset({"clin_fake", "clin_oprog"})
_NONE_EXP = re.compile(r"нет\s+в\s+(?!матриц)", re.IGNORECASE)


def expected_labels(exp: str) -> tuple[set[str], bool]:
    """(допустимые метки, ожидание неопределённое «?»). Учитывается только начало ожидания до пункта/скобки."""
    head = re.split(r"[(\d]", exp or "", maxsplit=1)[0].casefold()
    out: set[str] = set()
    for rx, lab in _EXP:
        if re.search(rx, head):
            out.add(lab)
            head = re.sub(rx, " ", head)
    return out, (exp or "").strip().startswith("?")


def simulated_picks(rec: dict[str, Any], choices: list[Any]) -> dict[str, tuple[str, str]]:
    """Выбор менеджера по разметке набора: {договор: (выбор, способ)}; способ — row_id | gold_none | strict."""
    from dms_core.constants import CLINIC_PICK_NONE
    from dms_core.tools.clinics import strict_name_match

    out: dict[str, tuple[str, str]] = {}
    gold_row = rec.get("clinic_row_id")
    gold_none = rec.get("clinic_pick") == CLINIC_PICK_NONE or rec.get("cls") in _NONE_CLS or \
        bool(_NONE_EXP.search(str(rec.get("exp") or "")))
    for ch in choices:
        row = gold_row.get(ch.contract_id) if isinstance(gold_row, dict) else gold_row
        if row:
            out[ch.contract_id] = (str(row), "row_id")
        elif gold_none:
            out[ch.contract_id] = (CLINIC_PICK_NONE, "gold_none")
        else:
            strict = next((r for r in ch.candidates if strict_name_match(ch.asked, r.clinic_name, r.legal_name)),
                          None)
            if strict is not None:
                out[ch.contract_id] = (strict.id, "strict")
    return out


def expected_for(rec: dict[str, Any]) -> tuple[set[str], bool] | dict[str, tuple[set[str], bool]]:
    """Ожидание записи: из expected_label (ревизия 7; для ГК — по договорам) или из текста exp."""
    el = rec.get("expected_label")
    if isinstance(el, dict):
        return {c: expected_labels(str(v)) for c, v in el.items()}
    if isinstance(el, str) and el:
        return expected_labels(el)
    return expected_labels(rec.get("exp", ""))


def labels_of(a: Any, want: Any) -> dict[str, str]:
    """{ключ: метка}: для ожидания по договорам — метки per_contract (нет договора — «-»), иначе общая метка."""
    if isinstance(want, dict):
        by_c = {pc.contract_id: (pc.label.value if pc.label else "-") for pc in a.per_contract}
        return {c: by_c.get(c, "-") for c in want}
    return {"": a.label.value if a.label else "-"}


def _mark(label: str, want: set[str], unknown: bool) -> str:
    if (not want or unknown) and label in ("allowed", "denied"):
        return "без метки в ожидании"
    if label == "allowed" and "allowed" not in want:
        return "ЛОЖНОЕ «можно»"
    if label == "denied" and "denied" not in want:
        return "ЛОЖНОЕ «нельзя»"
    return ""


class _Tally:
    def __init__(self) -> None:
        self.fa: list[str] = []
        self.fd: list[str] = []
        self.unlabelled: list[str] = []
        self.ok = self.scored = self.n = 0

    def add(self, rid: str, label: str, want: set[str], unknown: bool) -> str:
        self.n += 1
        mark = _mark(label, want, unknown)
        if mark == "без метки в ожидании":
            self.unlabelled.append(f"{rid}:{label}")
        elif mark == "ЛОЖНОЕ «можно»":
            self.fa.append(rid)
        elif mark == "ЛОЖНОЕ «нельзя»":
            self.fd.append(rid)
        if want:
            self.scored += 1
            self.ok += label in want
        return mark

    def scored_n(self) -> int:
        return self.n

    def line(self, n: int) -> str:
        return (f"вопросов {n}; совпало {self.ok}/{self.scored}; ложных «можно» {len(self.fa)} {self.fa}; "
                f"ложных «нельзя» {len(self.fd)} {self.fd}; можно/нельзя без метки в ожидании "
                f"{len(self.unlabelled)} {self.unlabelled}")


def main(argv: list[str]) -> int:
    from dms_core.engine import answer
    from dms_core.store import KitStore, default_path

    args = [a for a in argv if not a.startswith("--")]
    pick_mode, names = "--pick" in argv, "--names" in argv
    path = Path(args[0]) if args else derived_dir() / "review" / "review_w5_questions.jsonl"
    rows = [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]
    store = KitStore(default_path())
    base, picked = _Tally(), _Tally()
    offered = applied = 0
    how_count: dict[str, int] = {}
    # пары «вопрос × договор» с эталонной строкой перечня: выбор предложен / эталонная строка среди кандидатов
    gold_pairs = gold_offered = gold_in = 0
    not_offered: list[str] = []
    no_clinic_n, false_slot = 0, []
    for r in rows:
        ctx = ChatContext(group_id=r.get("g"), contract_id=r.get("c"), program=r.get("p"))
        a = None
        try:
            a = answer(r["q"], ctx, llm=None, store=store)
            label = a.label.value if a.label else "-"
            sc = a.scenario.value
        except Exception as exc:  # noqa: BLE001 — сбой одного вопроса не останавливает прогон
            label, sc = f"error:{type(exc).__name__}", "-"
        gold_row = r.get("clinic_row_id")
        if gold_row and a is not None:
            golds = gold_row if isinstance(gold_row, dict) else {r.get("c"): gold_row}
            by_c = {ch.contract_id: ch for ch in a.clinic_choices}
            for c, gid in golds.items():
                gold_pairs += 1
                ch = by_c.get(c)
                if ch is None:
                    not_offered.append(f"{r['id']}:{c}")
                    continue
                gold_offered += 1
                gold_in += any(x.id == gid for x in ch.candidates)
        exp_all = expected_for(r)
        wants = exp_all if isinstance(exp_all, dict) else {"": exp_all}
        labs = labels_of(a, exp_all) if a is not None else {k: label for k in wants}
        if r.get("no_clinic"):
            no_clinic_n += 1
            if a is not None and a.clinic_choices:
                false_slot.append(r["id"])
        for key, (want, unknown) in wants.items():
            rid = f"{r['id']}:{key}" if key else r["id"]
            mark = base.add(rid, labs[key], want, unknown)
            if mark or "--all" in argv:
                print(f"{rid:7} {sc:16} {labs[key]:13} exp={r.get('exp', '')!s:40} {mark}")
        if not pick_mode:
            continue
        # с выбором менеджера: пересчёт с clinic_picks; без выбора (нет кандидатов / строгого) — метка та же
        labs2, info = dict(labs), ""
        if a is not None and a.clinic_choices:
            offered += 1
            picks = simulated_picks(r, a.clinic_choices)
            if picks:
                applied += 1
                for _v, how in picks.values():
                    how_count[how] = how_count.get(how, 0) + 1
                try:
                    trace: dict[str, Any] = {}
                    a2 = answer(r["q"], ctx, llm=None, store=store, trace=trace,
                                clinic_picks={c: v for c, (v, _h) in picks.items()})
                    labs2 = labels_of(a2, exp_all)
                    rules = ",".join(f"{c}:{x.get('rule')}" for c, x in (trace.get("per_contract") or {}).items())
                    rows_by_id = {x.id: x for ch in a2.clinic_choices for x in ch.candidates}
                    counted = {ch.contract_id: ch.selected for ch in a2.clinic_choices}
                    desc = []
                    for c, (v, how) in picks.items():
                        nm = rows_by_id[v].clinic_name if names and v in rows_by_id else ""
                        ok = "учтён" if counted.get(c) == v else "НЕ учтён"
                        desc.append(f"{c}:{how}:{v}{(' «' + nm + '»') if nm else ''}({ok})")
                    info = f"выбор {' '.join(desc)} правило {rules}"
                except Exception as exc:  # noqa: BLE001
                    labs2, info = {k: f"error:{type(exc).__name__}" for k in wants}, ""
        for key, (want, unknown) in wants.items():
            rid = f"{r['id']}:{key}" if key else r["id"]
            mark2 = picked.add(rid, labs2[key], want, unknown)
            if (mark2 and mark2 != "без метки в ожидании") or (labs2[key] != labs[key] and "--all" in argv):
                print(f"  с выбором {rid:7} {labs[key]:13}→ {labs2[key]:13} exp={r.get('exp', '')!s:40} {mark2} "
                      f"{info}")
    print("без выбора: " + base.line(base.scored_n()))
    if gold_pairs:
        print(f"эталонная строка: пар {gold_pairs}, выбор предложен {gold_offered}, строка среди кандидатов "
              f"{gold_in}/{gold_offered}; выбор не предложен {len(not_offered)} {not_offered}")
    if no_clinic_n:
        print(f"вопросы без клиники (no_clinic): {no_clinic_n}, выбор клиники предложен (ложный слот) "
              f"{len(false_slot)} {false_slot}")
    if pick_mode:
        print(f"с выбором ({offered} ответов с выбором клиники, выбор сделан в {applied}: {how_count}): "
              + picked.line(len(rows)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
