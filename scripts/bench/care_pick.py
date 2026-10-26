"""Какую строку матрицы видов помощи выбирает guided.check_care для типовых услуг (этап w12-care).

По всем договорам и программам базы для ~40 типовых вопросов («приём терапевта», «МРТ», «онлайн-консультация»,
«вызов врача на дом», «госпитализация» …) печатается TSV: договор, программа, услуга, значение care_in_program,
выбранный вид помощи (название строки матрицы, до 90 знаков). Сравнение двух версий:
  uv run python scripts/bench/care_pick.py > до.tsv;  (после правки) > после.tsv;
  uv run python scripts/bench/care_pick.py --diff до.tsv после.tsv
Названия строк матрицы — производное от документов организаторов: вывод держать вне git (scratchpad / data/derived).
Запуск из app/ с DMS_DATA_DIR своей временной папки.
"""
from __future__ import annotations

import inspect
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
os.environ.setdefault("DMS_ENGINE_MODE", "guided")

# типовые вопросы менеджера: (ключ услуги, вопрос) — общие формулировки, без клиник и договоров
QUESTIONS: tuple[tuple[str, str], ...] = (
    ("терапевт", "Можно ли сотруднику на приём к терапевту?"),
    ("педиатр", "Ребёнку нужен приём педиатра, это входит?"),
    ("невролог", "Сотрудник хочет попасть к неврологу, входит ли это?"),
    ("гинеколог", "Положен ли приём гинеколога?"),
    ("офтальмолог", "Входит ли консультация офтальмолога?"),
    ("лор", "Можно ли сходить к лору?"),
    ("кардиолог", "Входит ли приём кардиолога в программу?"),
    ("психотерапевт", "Покрывается ли консультация психотерапевта?"),
    ("стоматолог", "Можно ли сотруднику к стоматологу?"),
    ("пломба", "Входит ли постановка пломбы?"),
    ("чистка зубов", "Входит ли профессиональная чистка зубов?"),
    ("МРТ", "Сотрудник хочет сделать МРТ, входит ли это в программу?"),
    ("КТ", "Можно ли сделать КТ по программе?"),
    ("УЗИ", "Входит ли УЗИ брюшной полости?"),
    ("рентген", "Можно ли сделать рентген по ДМС?"),
    ("ЭКГ", "Входит ли ЭКГ в программу?"),
    ("гастроскопия", "Покрывается ли гастроскопия?"),
    ("маммография", "Входит ли маммография?"),
    ("анализы", "Можно ли сдать анализы по программе?"),
    ("анализ крови", "Входит ли общий анализ крови?"),
    ("массаж", "Входит ли массаж в программу?"),
    ("физиотерапия", "Положена ли физиотерапия?"),
    ("ЛФК", "Можно ли заниматься ЛФК по ДМС?"),
    ("мануальная терапия", "Входит ли мануальная терапия?"),
    ("вызов врача на дом", "Можно ли вызвать врача на дом?"),
    ("терапевт на дом", "Можно ли вызвать терапевта на дом?"),
    ("госпитализация", "Положена ли плановая госпитализация?"),
    ("стационар", "Покрывается ли лечение в стационаре?"),
    ("экстренная госпитализация", "Входит ли экстренная госпитализация?"),
    ("скорая", "Можно ли вызвать скорую помощь по ДМС?"),
    ("телемедицина", "Входит ли телемедицинская консультация?"),
    ("онлайн терапевт", "Можно ли получить онлайн-консультацию терапевта?"),
    ("дистанционно врач", "Можно ли проконсультироваться с врачом дистанционно через интернет?"),
    ("вакцинация", "Входит ли вакцинация от гриппа?"),
    ("прививка", "Можно ли сделать прививку по ДМС?"),
    ("аптека", "Покрываются ли лекарства по программе?"),
    ("реабилитация", "Входит ли реабилитация?"),
    ("справка", "Можно ли получить справку для бассейна?"),
    ("процедуры", "Входят ли процедуры, назначенные врачом?"),
    ("капельница", "Входит ли капельница?"),
    ("дерматолог", "Входит ли приём дерматолога?"),
    ("хирург", "Можно ли попасть к хирургу?"),
)


def rows() -> list[tuple[str, str, str, str, str]]:
    from dms_core.guided import _program_names, _Run, bound_executor, check_care, name_context
    from dms_core.scenarios.route import rule_slots
    from dms_core.schemas import ChatContext
    from dms_core.store import KitStore, default_path

    st = KitStore(default_path())
    with_question = "question" in inspect.signature(check_care).parameters
    out: list[tuple[str, str, str, str, str]] = []
    for c in st.contracts():
        cid = c.id
        probe = _Run(cid=cid, st=st, execute=bound_executor(cid, st))
        for program in _program_names(probe):
            ctx = ChatContext(contract_id=cid, program=program)
            for key, q in QUESTIONS:
                slots = rule_slots(q, ctx, st)
                service = slots.get("service") or slots.get("care_type")
                care = slots.get("care_type") or slots.get("service")
                if not service:
                    out.append((cid, program, key, "-", ""))
                    continue
                run = _Run(cid=cid, st=st, execute=bound_executor(cid, st))
                skip = [*name_context(st, cid, slots)]
                kw = {"question": q} if with_question else {}
                cc = check_care(run, program, care, service, skip=skip, **kw)
                out.append((cid, program, key, cc.value, (cc.name or "")[:90].replace("\t", " ")))
    return out


def diff(a: Path, b: Path) -> int:
    """Изменившиеся строки двух прогонов: договор, программа, услуга, было → стало."""
    def load(p: Path) -> dict[tuple[str, str, str], tuple[str, str]]:
        d: dict[tuple[str, str, str], tuple[str, str]] = {}
        for line in p.read_text(encoding="utf-8").splitlines():
            f = line.split("\t")
            if len(f) == 5:
                d[(f[0], f[1], f[2])] = (f[3], f[4])
        return d
    old, new = load(a), load(b)
    n = 0
    for k in sorted(set(old) | set(new)):
        if old.get(k) != new.get(k):
            n += 1
            o, w = old.get(k, ("", "")), new.get(k, ("", ""))
            print(f"{k[0]}\t{k[1][:40]}\t{k[2]}\t{o[0]} {o[1][:60]}\t→\t{w[0]} {w[1][:60]}")
    print(f"# изменений: {n} из {len(new)}")
    return 0


def main(argv: list[str]) -> int:
    if argv[:1] == ["--diff"] and len(argv) == 3:
        return diff(Path(argv[1]), Path(argv[2]))
    for r in rows():
        print("\t".join(r))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
