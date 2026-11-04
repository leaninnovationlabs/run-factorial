"""A bounded map for counts kept in process memory, keyed on what a stranger chooses."""

from __future__ import annotations

from collections import OrderedDict
from typing import Final

__all__ = ["CAPACITY", "BoundedLRU"]

#: Keys remembered at once. The one written longest ago goes first, so a flood of new keys costs
#: memory up to here and no further.
CAPACITY: Final = 10_000


class BoundedLRU[K, V]:
    def __init__(self, capacity: int = CAPACITY) -> None:
        self._capacity = capacity
        self._items: OrderedDict[K, V] = OrderedDict()

    def get(self, key: K, default: V) -> V:
        return self._items.get(key, default)

    def pop(self, key: K, default: V) -> V:
        return self._items.pop(key, default)

    def put(self, key: K, value: V) -> None:
        """Write `value` as the newest key, and drop the oldest past the capacity."""
        self._items.pop(key, None)
        self._items[key] = value
        while len(self._items) > self._capacity:
            self._items.popitem(last=False)
