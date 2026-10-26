"""Сверка автонумерации read_docx с LibreOffice на всех docx комплекта (план §14: ≥ 99 % на каждом файле).
Нужны комплект (data/raw/) и soffice, иначе SKIP. Выгрузки LO кэшируются в data/derived/lo_txt_cache/;
отчёт по файлам пишет `uv run python -m dms_core.io.lo_check` → data/derived/numbering_check.json."""
from __future__ import annotations

from dms_core.config import derived_dir
from dms_core.io.docx_reader import iter_all_paras, read_docx
from dms_core.io.lo_check import check_file, compare, lo_text
from helpers import kit_or_skip, soffice_or_skip


def test_numbering_matches_libreoffice_on_every_kit_docx():
    kit = kit_or_skip()
    soffice = soffice_or_skip()
    files = sorted(kit.rglob("*.docx"))
    assert len(files) == 27
    bad = []
    checked = 0
    for f in files:
        r = check_file(f, soffice, cache_dir=derived_dir() / "lo_txt_cache")
        if r.status == "no_numbering":          # ГК 2: автонумерации нет — не проверяется и в долю не входит
            continue
        checked += 1
        if r.rate is None or r.rate < 0.99:
            bad.append((str(f.relative_to(kit)), r.rate, r.mismatches[:3]))
        # каждый скрытый абзац, о котором сообщило чтение, нашёл свою метку у LO, лишних меток LO нет
        assert r.hidden_skipped <= r.hidden_declared
    assert not bad, bad
    assert checked == 19


def test_mutation_lost_paragraphs_lower_rate_on_kit():
    """Мутация: удалить из нашего разбора 10 нумерованных абзацев — доля обязана упасть (ГК 1 и c09)."""
    kit = kit_or_skip()
    soffice = soffice_or_skip()
    targets = [kit / "ГК 1" / "Компания 1", kit / "ГК 3" / "Компания 9"]
    for folder in targets:
        f = next(x for x in sorted(folder.glob("*.docx")) if "Договор" in x.name or "Программ" in x.name)
        doc = read_docx(f)
        paras = [(x.number or "", x.text) for x in iter_all_paras(doc)]
        drop = {i for i, (lbl, t) in enumerate(paras) if lbl[:1].isdigit() and t.strip()}
        drop = set(sorted(drop)[5:15])
        hidden = [(pos - sum(1 for i in drop if i < pos), lbl) for pos, lbl in doc.skipped_hidden]
        lo = lo_text(f, soffice, cache_dir=derived_dir() / "lo_txt_cache")
        base = compare(paras, lo, hidden=doc.skipped_hidden)
        res = compare([x for i, x in enumerate(paras) if i not in drop], lo, hidden=hidden)
        assert base.rate == 1.0
        assert res.rate is not None and res.rate <= 0.99 and res.lo_extra >= 9, (f.name, res.rate, res.lo_extra)
