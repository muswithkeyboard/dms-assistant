"""Ошибки чтения файлов. Сообщение — по-русски и годится для показа пользователю (`docs/rules.md` п. 11);
текста документа в сообщении нет (`docs/rules.md` п. 4)."""
from __future__ import annotations


class InputError(ValueError):
    """Файл нельзя прочитать: code — машинный код (bad_file, too_large, no_text_layer, …), message — по-русски."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message
