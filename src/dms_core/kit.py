"""Опись комплекта организаторов: ГК → юрлица (договоры) → документы (допущение A2, план §3.1).

Привязка — по папкам «ГК N / Компания M»: одно юрлицо = один договор. Файлы прямо в папке ГК — общие для группы.
Тип документа — по имени файла (config/doc_types.yaml); scope «group» переносит документ на уровень ГК, даже если
он лежит в папке компании (правила НС ГК 1 лежат у Компании 2, но относятся ко всей группе).
Содержимое не читается — только имена, размеры и sha256. Разбор — io/ и adapters/ (волна 1).
"""
from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .config import kit_dir, load_yaml

_GROUP_RE = re.compile(r"^ГК\s*(\d+)$")
_COMPANY_RE = re.compile(r"^Компания\s*(\d+)$")
SUPPORTED = {".docx": "docx", ".xlsx": "xlsx", ".pdf": "pdf", ".txt": "txt"}


def nfc(s: str) -> str:
    """macOS отдаёт имена файлов в NFD («и» + «̆»); сравниваем и храним в NFC."""
    return unicodedata.normalize("NFC", s)


@dataclass
class KitFile:
    path: str              # относительно kit_dir()
    title: str             # имя файла без расширения
    fmt: str
    doc_type: str
    scope: str             # contract | group
    size: int
    sha256: str


@dataclass
class KitContract:
    id: str                # c01 … c13 — по номеру компании
    group_id: str
    legal_entity: str      # «Компания 1»
    files: list[KitFile] = field(default_factory=list)


@dataclass
class KitGroup:
    id: str                # gk1
    name: str              # «ГК 1»
    files: list[KitFile] = field(default_factory=list)          # общие для ГК
    contracts: list[KitContract] = field(default_factory=list)


def classify(filename: str) -> tuple[str, str]:
    """(doc_type, scope) по config/doc_types.yaml — первое совпавшее правило."""
    cfg = load_yaml("doc_types.yaml")
    name = nfc(filename).casefold()
    for rule in cfg["rules"]:
        if re.search(rule["pattern"], name):
            return rule["doc_type"], rule["scope"]
    return cfg["default"]["doc_type"], cfg["default"]["scope"]


def _sha256(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _file(p: Path, root: Path) -> KitFile | None:
    fmt = SUPPORTED.get(p.suffix.lower())
    if fmt is None or p.name.startswith((".", "~$")):
        return None
    doc_type, scope = classify(p.name)
    return KitFile(path=nfc(str(p.relative_to(root))), title=nfc(p.stem), fmt=fmt, doc_type=doc_type,
                   scope=scope, size=p.stat().st_size, sha256=_sha256(p))


def scan(root: Path | None = None) -> list[KitGroup]:
    """Опись комплекта. Нет папки — пустой список (вызывающий решает, ошибка это или SKIP)."""
    root = root or kit_dir()
    if not root.is_dir():
        return []
    groups: list[KitGroup] = []
    for gdir in sorted(root.iterdir(), key=lambda p: nfc(p.name)):
        m = _GROUP_RE.match(nfc(gdir.name)) if gdir.is_dir() else None
        if not m:
            continue
        group = KitGroup(id=f"gk{m.group(1)}", name=nfc(gdir.name))
        shared: list[KitFile] = []
        for item in sorted(gdir.iterdir(), key=lambda p: nfc(p.name)):
            if item.is_file():
                f = _file(item, root)
                if f:
                    group.files.append(f)
                continue
            cm = _COMPANY_RE.match(nfc(item.name))
            if not cm:
                continue
            contract = KitContract(id=f"c{int(cm.group(1)):02d}", group_id=group.id, legal_entity=nfc(item.name))
            for p in sorted(item.rglob("*"), key=lambda p: nfc(str(p))):
                f = _file(p, root) if p.is_file() else None
                if f is None:
                    continue
                (shared if f.scope == "group" else contract.files).append(f)
            group.contracts.append(contract)
        group.files.extend(shared)
        group.contracts.sort(key=lambda c: c.id)
        groups.append(group)
    groups.sort(key=lambda g: int(g.id[2:]))
    return groups


def to_dict(groups: list[KitGroup]) -> list[dict]:
    return [asdict(g) for g in groups]
