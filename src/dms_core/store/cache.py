"""Кэш в памяти с пределом по «размеру» (числу пунктов) — для пунктов договоров (store.KitStore.clauses) и поисковых
индексов (index.search.get_index); круг 2 ревизии w16: группа из 100 договоров.

Обычный LRU при обходе группы, которая больше предела, вытесняет договор, который понадобится через несколько шагов
этого же ответа, — и каждый вопрос пересобирает всё. Здесь вытесняются только записи, к которым не обращались
дольше PROTECT_SEC; если место так не освободить, новая запись не кэшируется (собирается, используется и
отбрасывается). Тогда часть группы, поместившаяся в предел, остаётся в памяти между вопросами.
"""
from __future__ import annotations

import threading
import time
from collections import OrderedDict
from collections.abc import Callable

PROTECT_SEC = 120.0      # запись, нужная в последние 2 минуты (ответ по большой группе), не вытесняется


class BoundedCache[V]:
    def __init__(self, limit: int, size: Callable[[V], int], protect_sec: float = PROTECT_SEC) -> None:
        self.limit, self._size, self.protect = limit, size, protect_sec
        self._items: OrderedDict[str, tuple[float, V]] = OrderedDict()
        self._total = 0
        self._lock = threading.Lock()

    def get(self, key: str) -> V | None:
        with self._lock:
            got = self._items.get(key)
            if got is None:
                return None
            self._items[key] = (time.monotonic(), got[1])
            self._items.move_to_end(key)
            return got[1]

    def put(self, key: str, value: V) -> bool:
        """Положить value; False — не поместилось (всё остальное нужно недавно), value не кэшируется."""
        n = self._size(value)
        with self._lock:
            self._drop(key)
            now = time.monotonic()
            while self._total + n > self.limit and self._items:
                old_key, (used, _v) = next(iter(self._items.items()))
                if now - used < self.protect:
                    break
                self._drop(old_key)
            if self._total + n > self.limit:
                return False
            self._items[key] = (now, value)
            self._total += n
            return True

    def pop(self, key: str) -> None:
        with self._lock:
            self._drop(key)

    def snapshot(self) -> list[str]:
        with self._lock:
            return list(self._items)

    def _drop(self, key: str) -> None:
        got = self._items.pop(key, None)
        if got is not None:
            self._total -= self._size(got[1])
