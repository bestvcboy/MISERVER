from pathlib import Path

from core import Journal


class _ReadonlyHistory:
    __slots__ = ("__reader",)

    def __init__(self, root):
        reader = object.__new__(Journal)
        reader.root = Path(root)
        reader.current = None
        reader.ranges = {}
        self.__reader = reader

    def history(self, since, until, points=600):
        return self.__reader.history(since, until, points)


def readonly_history(root):
    return _ReadonlyHistory(root)
