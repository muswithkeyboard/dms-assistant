"""Противоречия документов внутри договора (допущение A3, `docs/rules.md` п. 2, ТЗ п. 8.1).

Вход — проверенный черновик (citations.verify): в `draft.contradictions` остались только пары с двумя живыми
цитатами. Для каждой пары по config/precedence.yaml (через precedence.py):
- уровни разные → позиция по старшему документу + предупреждение с обеими цитатами (и предложение письма в СК);
- уровни равные → «источники противоречат друг другу» (статус contradictory, ТЗ п. 8.1), показаны обе цитаты.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .citations import Verified
from .constants import StatementKind
from .precedence import senior
from .schemas import Citation, Statement

UNKNOWN_LEVEL = 50   # уровень документа, которого нет в индексе (как `other` в precedence.yaml)


@dataclass
class Conflict:
    a: Citation
    b: Citation
    senior: Citation | None      # None — уровни равны
    note: str = ""

    @property
    def equal(self) -> bool:
        return self.senior is None


@dataclass
class ConflictReport:
    conflicts: list[Conflict] = field(default_factory=list)

    @property
    def contradictory(self) -> bool:
        """Статус «источники противоречат друг другу» (ТЗ п. 8.1) — при любом показанном противоречии.

        Разные уровни (A3): позицию занимаем по старшему документу (метка — «можно при условиях (нужно
        подтверждение СК)»), но противоречие показано обеими цитатами и статус — contradictory, как в эталоне К17
        (решение автора 24.10.2026, docs/status/stage-w2-verify.md)."""
        return bool(self.conflicts)

    @property
    def resolved_by_precedence(self) -> bool:
        return any(not c.equal for c in self.conflicts)

    def warnings(self) -> list[Statement]:
        """Предупреждения для ответа: каждое — с обеими цитатами (старшая первой)."""
        out: list[Statement] = []
        for c in self.conflicts:
            if c.senior is None:
                text = "Документы одного уровня противоречат друг другу; нужна позиция страховой компании."
                cits = [c.a, c.b]
            else:
                junior = c.b if c.senior is c.a else c.a
                text = (f"Документы противоречат друг другу; позиция — по старшему документу «{c.senior.doc_title}». "
                        "Рекомендуем подтвердить у страховой компании.")
                cits = [c.senior, junior]
            if c.note:
                text = f"{text} {c.note}"
            out.append(Statement(text=text, citations=cits, kind=StatementKind.limitation))
        return out


def analyze(v: Verified) -> ConflictReport:
    """Разбор противоречий проверенного черновика по старшинству документов."""
    report = ConflictReport()
    for pair in v.draft.contradictions:
        a, b = v.citation(pair.a), v.citation(pair.b)
        if a is None or b is None:        # verify уже отбросил такие пары; защита от ручного вызова
            continue
        la = v.doc_levels.get(a.doc_id, UNKNOWN_LEVEL)
        lb = v.doc_levels.get(b.doc_id, UNKNOWN_LEVEL)
        side = senior(la, lb)
        report.conflicts.append(Conflict(a=a, b=b, senior=None if side is None else (a, b)[side], note=pair.note))
    return report
