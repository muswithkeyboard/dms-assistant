"""Формат идентификаторов строк клиник у всех адаптеров (docs/CONTRACTS.md §1, ревизия волны 1):
source_ref = «<doc_id>#t<таблица>r<строка>» (docx) или «<doc_id>#<лист>!<строка>» (xlsx); ClinicRow.id начинается
с source_ref, суффикс — только «#<k>» (несколько видов помощи) или «#v<n>» (строка размножена по вариантам Зетты)."""
from __future__ import annotations

import re
import sys
import tempfile
from functools import cache
from pathlib import Path

from dms_core.adapters import parse_contract
from dms_core.adapters import renaissance as ren
from helpers import ROOT, kit_or_skip

_DOCX_REF = re.compile(r"^(?P<doc>c\d\d\.[a-z_]+(?:\.\d+)?)#t\d+r\d+$")
_XLSX_REF = re.compile(r"^(?P<doc>c\d\d\.[a-z_]+(?:\.\d+)?)#[^#!]+!\d+$")
_SUFFIX = re.compile(r"^(#\d+|#v\d+)?$")


def _check(rows) -> None:
    for r in rows:
        m = _DOCX_REF.match(r.source_ref) or _XLSX_REF.match(r.source_ref)
        assert m and m.group("doc") == r.doc_id, r.source_ref
        assert r.id.startswith(r.source_ref) and _SUFFIX.match(r.id[len(r.source_ref):]), (r.id, r.source_ref)
    assert len({r.id for r in rows}) == len(rows)


def test_fixture_renaissance_ids():
    sys.path.insert(0, str(ROOT / "tests" / "fixtures"))
    from ren_fixture import build

    kc, root = build(Path(tempfile.mkdtemp(prefix="ren_ids_")))
    rows = ren.parse(kc, root).clinics
    assert rows
    _check(rows)


@cache
def _kit():
    from dms_core.kit import scan

    root = kit_or_skip()
    return root, [kc for g in scan(root) for kc in g.contracts]


def test_kit_all_adapters_ids():
    """Зетта (c01–c04), Ренессанс (c05–c08), СОГАЗ (c09+): один формат ссылок на строку клиники."""
    root, contracts = _kit()
    seen = set()
    for kc in contracts:
        pc = parse_contract(kc, root)
        if not pc.clinics:
            continue
        _check(pc.clinics)
        seen.add(pc.contract.insurer)
    assert len(seen) >= 3, seen
