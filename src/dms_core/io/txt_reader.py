"""Чтение txt: UTF-8 (с BOM или без), иначе cp1251 (письма и выгрузки из русской Windows)."""
from __future__ import annotations

from pathlib import Path

from dms_core.io.errors import InputError

MAX_TXT_BYTES = 20 * 2**20


def decode_text(data: bytes) -> str:
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return data.decode("cp1251", errors="replace")


def read_txt(path: Path | str, max_bytes: int = MAX_TXT_BYTES) -> str:
    """Текст файла с нормализованными переводами строк (\\r\\n, \\r → \\n)."""
    path = Path(path)
    try:
        size = path.stat().st_size
    except OSError as e:
        raise InputError("bad_file", "Текстовый файл не открывается.") from e
    if size > max_bytes:
        raise InputError("too_large", f"Текстовый файл больше {max_bytes // 2**20} МБ.")
    text = decode_text(path.read_bytes())
    return text.replace("\r\n", "\n").replace("\r", "\n")
