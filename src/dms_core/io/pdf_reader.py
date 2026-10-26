"""Чтение PDF (docs/CONTRACTS.md §2): текст по страницам, без OCR. По образцу Justmedit
(reference/justmedit-code/pdf_text.py): страницу pdfminer изнутри не прервать, поэтому разбор идёт в дочернем
процессе `python -m dms_core.io.pdf_reader`, который убивается по жёсткому таймауту. Файл передаётся через stdin,
результат — JSON через stdout; stderr дочернего процесса (предупреждения pdfminer, где может быть содержимое
файла) отбрасывается и в журнал не попадает.

В комплекте PDF — только правила страхования (ГК 1); правила СК — не условия договора (`docs/rules.md` п. 0а), их
читают как справочный документ. Нет текстового слоя (скан) → InputError no_text_layer.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

from dms_core.io.errors import InputError

MAX_PDF_BYTES = 50 * 2**20
MAX_PAGES = 500
MAX_SECONDS = 120.0                 # правила СК (ГК 1, ~1 МБ) читаются за единицы секунд
HARD_TIMEOUT_EXTRA = 5.0            # запас на запуск дочернего процесса (импорт pdfplumber)
MAX_MEMORY_BYTES = 2 * 2**30
PDF_MAGIC = b"%PDF-"
_SRC_DIR = str(Path(__file__).resolve().parents[2])

NO_TEXT_LAYER = ("В PDF нет текстового слоя (похоже на скан): распознавание не поддерживается — "
                 "загрузите PDF с текстом или docx.")


def _limit_child_resources(max_seconds: float) -> None:
    """Память и процессорное время дочернего процесса (Linux; в macOS предел памяти может не действовать)."""
    try:
        import resource
        if sys.platform.startswith("linux"):
            resource.setrlimit(resource.RLIMIT_AS, (MAX_MEMORY_BYTES, MAX_MEMORY_BYTES))
        cpu = int(max_seconds) + 2
        resource.setrlimit(resource.RLIMIT_CPU, (cpu, cpu))
    except (ImportError, ValueError, OSError):
        pass


def _extract_in_process(data: bytes, max_pages: int, max_seconds: float) -> dict[str, object]:
    """Разбор в текущем процессе (вызывается только в дочернем процессе)."""
    import io
    import logging

    import pdfplumber

    logging.getLogger("pdfminer").setLevel(logging.ERROR)
    t0 = time.monotonic()
    pages: list[str] = []
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        total = len(pdf.pages)
        for page in pdf.pages[:max_pages]:
            if time.monotonic() - t0 > max_seconds:
                return {"status": "timeout"}
            pages.append(page.extract_text() or "")
    return {"status": "ok", "pages": pages, "pages_total": total}


def _worker_main() -> None:
    """Точка входа дочернего процесса: PDF из stdin, JSON в stdout. Подробности ошибки наружу не отдаются."""
    max_pages, max_seconds = int(sys.argv[1]), float(sys.argv[2])
    _limit_child_resources(max_seconds)
    try:
        out = _extract_in_process(sys.stdin.buffer.read(), max_pages, max_seconds)
    except Exception:  # noqa: BLE001 — любой сбой разбора = нечитаемый PDF; подробности могут содержать данные
        out = {"status": "error"}
    sys.stdout.write(json.dumps(out, ensure_ascii=True))


def _bad_pdf() -> InputError:
    return InputError("bad_pdf", "Не удалось прочитать PDF: файл повреждён, зашифрован или это не PDF.")


def read_pdf(path: Path | str, *, max_pages: int = MAX_PAGES, max_seconds: float = MAX_SECONDS) -> list[str]:
    """Текст по страницам. Ошибки — InputError: bad_pdf, too_large, pdf_too_complex (таймаут), no_text_layer."""
    path = Path(path)
    try:
        data = path.read_bytes()
    except OSError as e:
        raise _bad_pdf() from e
    if not data[:1024].lstrip().startswith(PDF_MAGIC):
        raise _bad_pdf()
    if len(data) > MAX_PDF_BYTES:
        raise InputError("too_large", f"PDF больше {MAX_PDF_BYTES // 2**20} МБ.")
    env = {k: v for k, v in os.environ.items() if k in ("PATH", "LANG", "LC_ALL", "SYSTEMROOT", "TMPDIR")}
    env["PYTHONPATH"] = _SRC_DIR + (os.pathsep + os.environ["PYTHONPATH"] if os.environ.get("PYTHONPATH") else "")
    env["PYTHONIOENCODING"] = "utf-8"
    cmd = [sys.executable, "-m", "dms_core.io.pdf_reader", str(int(max_pages)), str(float(max_seconds))]
    try:
        proc = subprocess.run(cmd, input=data, capture_output=True, timeout=max_seconds + HARD_TIMEOUT_EXTRA,
                              check=False, env=env)
    except subprocess.TimeoutExpired as e:      # subprocess.run убивает дочерний процесс при таймауте
        raise InputError("pdf_too_complex", f"PDF не удалось разобрать за {max_seconds:g} с.") from e
    except OSError as e:
        raise InputError("pdf_unavailable", "Разбор PDF сейчас недоступен.") from e
    try:
        out = json.loads(proc.stdout.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as e:
        raise _bad_pdf() from e
    if not isinstance(out, dict):
        raise _bad_pdf()
    if out.get("status") == "timeout":
        raise InputError("pdf_too_complex", f"PDF не удалось разобрать за {max_seconds:g} с.")
    if out.get("status") != "ok":
        raise _bad_pdf()
    pages = [str(p or "") for p in out.get("pages") or []]
    if not any(p.strip() for p in pages):
        raise InputError("no_text_layer", NO_TEXT_LAYER)
    return pages


if __name__ == "__main__":
    _worker_main()
