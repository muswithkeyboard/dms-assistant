"""Бессмысленный ввод (круг 2 ревизии w16): «фывапролд», «asdf», «123», «ааааа» — не вопрос, а случайный набор.

Такой ввод не ищется по документам (поиск находил случайные пункты по обрывкам), а получает ответ «не понял вопрос»
с примером. Набор в английской раскладке («Dj[jlbn kb VHN?» → «Входит ли МРТ?») распознаётся отдельно: ответ
предлагает тот же вопрос в русской раскладке кнопкой.

Правило — только по форме слов, без догадок о смысле, и с запасом в сторону «это вопрос»: слово бессмысленно, только
если его нет в словаре pymorphy3 и в нём есть явный признак набора — ряд клавиатуры из 4+ знаков подряд, один знак
3+ раза подряд, нет гласных (длиннее аббревиатуры); слова с большой буквы и аббревиатуры (названия клиник, «МРТ») — не бессмысленны.
Ввод бессмыслен, если в нём нет букв или все слова бессмысленны. Медицинские слова вне словаря («колоноскопия»,
«онкомаркеры») признаков набора не имеют и проходят как вопрос.
"""
from __future__ import annotations

import re

from .index.morph import _analyzer

_ROWS = ("йцукенгшщзхъ", "фывапролджэ", "ячсмитьбю", "qwertyuiop", "asdfghjkl", "zxcvbnm", "1234567890")
_EN = "qwertyuiop[]asdfghjkl;'zxcvbnm,.`"
_RU = "йцукенгшщзхъфывапролджэячсмитьбюё"
# заглавные — по буквам (у «[» заглавной нет) и по знакам с Shift («{» — «Х», «:» — «Ж», «<» — «Б»)
_LAYOUT = str.maketrans({**{e.upper(): r.upper() for e, r in zip(_EN, _RU, strict=True) if e.isalpha()},
                         **dict(zip('{}:"<>~', "ХЪЖЭБЮЁ", strict=True)), **dict(zip(_EN, _RU, strict=True))})
_VOWELS = set("аеёиоуыэюяaeiouy")
_WORD = re.compile(r"[a-zа-яё]+", re.IGNORECASE)
KEYBOARD_RUN = 4          # столько знаков ряда клавиатуры подряд — набор, а не слово
LAYOUT_KNOWN_SHARE = 0.6
LAYOUT_WORD_MIN = 4       # такой длины словарное слово нужно, чтобы считать текст набранным не в той раскладке
ABBREV_MAX = 4            # слово без гласных до 4 букв — аббревиатура строчными («лфк», «экг»), не набор  # доля словарных слов после перевода раскладки, чтобы предложить русский вариант


def _known(word: str) -> bool:
    return bool(re.search("[а-яё]", word, re.IGNORECASE)) and _analyzer().word_is_known(word.casefold())


def _keyboard_run(word: str) -> bool:
    w = word.casefold()
    return any(w[i:i + KEYBOARD_RUN] in row for row in _ROWS for i in range(len(w) - KEYBOARD_RUN + 1))


def _mashed(word: str) -> bool:
    """Слово — случайный набор: не словарное, не имя (с большой буквы / аббревиатура) и есть признак набора."""
    if _known(word):
        return False
    w = word.casefold()
    if _keyboard_run(w) or re.search(r"(.)\1\1", w):
        return True                          # «Фывапролд» с большой буквы — тоже набор
    # слово с большой буквы без гласных — аббревиатура или название («МРТ», «ЕМС»), не набор
    return not word[:1].isupper() and len(w) > ABBREV_MAX and not (set(w) & _VOWELS)


def is_gibberish(text: str) -> bool:
    """Ввод — не вопрос: ни букв, ни цифр («??») или все слова — случайный набор (_mashed). Одни цифры
    («2.6.1», «№ 123/45») — номер пункта или договора, это ищется."""
    words = _WORD.findall(text or "")
    if not words:
        return not re.search(r"\d", text or "")
    return all(_mashed(w) for w in words)


def wrong_layout(text: str) -> str | None:
    """Текст, набранный в английской раскладке вместо русской, → тот же текст по-русски; иначе None.
    Признак — в тексте нет кириллицы, а после перевода раскладки большинство слов словарные."""
    t = (text or "").strip()
    # кириллица в тексте или код из буквы и цифр («J06», «K+31», «c05») — набран намеренно латиницей
    if not t or re.search("[а-яё]", t, re.IGNORECASE) or re.search(r"[a-z][+\-]?\d", t, re.IGNORECASE):
        return None
    ru = t.translate(_LAYOUT)
    words = [w for w in _WORD.findall(ru) if len(w) >= 2]
    known = [w for w in words if _known(w)]
    # хотя бы одно словарное слово от LAYOUT_WORD_MIN букв: короткие латинские слова («in», «vs», «tot») по
    # случайности переводятся в словарные («шт», «мы», «еще»)
    if not words or len(known) / len(words) < LAYOUT_KNOWN_SHARE or \
            not any(len(w) >= LAYOUT_WORD_MIN for w in known):
        return None
    # «?» русской раскладки (Shift+7) в английской — «&»
    return ru.replace("&", "?")


def smalltalk(text: str) -> str | None:
    """Ответ на обычную реплику («привет», «спасибо», «что ты умеешь»), если сообщение целиком — реплика
    (config/texts_ru/reply.yaml smalltalk); иначе None. «Спасибо, а МРТ входит?» — вопрос, не реплика."""
    from .config import load_yaml

    t = re.sub(r"[!.,?)(:;\s]+", " ", (text or "").casefold()).strip()
    if not t:
        return None
    for pat, reply in (load_yaml("texts_ru/reply.yaml") or {}).get("smalltalk") or []:
        if re.fullmatch(pat, t):
            return str(reply)
    return None
