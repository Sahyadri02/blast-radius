"""Naming of sqlglot's import-time tables, shared by the mapping run and the selector.

walk_tables(modules) yields (name, owner, attr, value) for every module global and
every class attribute (classes defined in sqlglot modules, nested classes included).
Names: "<module>.<global>" and "<module>:<class qualname>.<attr>".
krepr(key) gives a stable text form of a table key.
"""
import types
from enum import Enum

SKIP = {"__dict__", "__weakref__", "__module__", "__doc__", "__annotations__",
        "__firstlineno__", "__static_attributes__", "__orig_bases__", "__parameters__"}


def krepr(k):
    t = type(k)
    if k is None or t in (bool, int, float):
        return "v:" + repr(k)
    if t is str:
        return "s:" + k
    if isinstance(k, Enum):
        return "e:" + type(k).__module__ + "." + type(k).__qualname__ + "." + k.name
    if isinstance(k, type):
        return "t:" + (k.__module__ or "") + "." + k.__qualname__
    if t is tuple:
        return "(" + ",".join(krepr(x) for x in k) + ")"
    if isinstance(k, str):
        return "S:" + t.__qualname__ + ":" + str(k)
    if isinstance(k, (types.FunctionType, types.BuiltinFunctionType)):
        return "f:" + (getattr(k, "__module__", "") or "") + "." + getattr(k, "__qualname__", "")
    return None  # no stable name


def _classes_of(cls, prefix, depth, out, seen):
    if depth > 6 or id(cls) in seen:
        return
    seen.add(id(cls))
    out.append(cls)
    for k, v in list(vars(cls).items()):
        if isinstance(v, type) and v.__qualname__.startswith(cls.__qualname__ + "."):
            _classes_of(v, prefix, depth + 1, out, seen)


def walk_tables(modules):
    """modules: {name: module}. Yields (name, owner, attr, value, is_class)."""
    for mname in sorted(modules):
        mod = modules[mname]
        if mod is None:
            continue
        g = vars(mod)
        classes = []
        seen = set()
        for k in sorted(g):
            if k.startswith("__") and k != "__all__":
                continue
            v = g[k]
            if isinstance(v, type) and getattr(v, "__module__", None) == mname:
                _classes_of(v, mname, 0, classes, seen)
            else:
                yield mname + "." + k, mod, k, v, False
        for cls in classes:
            for k, v in list(vars(cls).items()):
                if k in SKIP:
                    continue
                if isinstance(v, type) and v.__qualname__.startswith(cls.__qualname__ + "."):
                    continue
                yield mname + ":" + cls.__qualname__ + "." + k, cls, k, v, True


# ---------------------------------------------------------------- mapping-time tracking
REC = [None]   # the access set of the current context (or None: not recording)


class TDict(dict):
    """A dict that records which keys are read (and when it is read whole)."""
    __slots__ = ("_n",)

    def _r(self, k):
        s = REC[0]
        if s is not None:
            s.add((self._n, k))

    def _all(self):
        s = REC[0]
        if s is not None:
            s.add((self._n, "*"))

    def __getitem__(self, k):
        self._r(k)
        return dict.__getitem__(self, k)

    def get(self, k, d=None):
        self._r(k)
        return dict.get(self, k, d)

    def __contains__(self, k):
        self._r(k)
        return dict.__contains__(self, k)

    def __len__(self):
        s = REC[0]
        if s is not None:
            s.add((self._n, "#len"))
        return dict.__len__(self)

    def __iter__(self):
        self._all(); return dict.__iter__(self)

    def keys(self):
        self._all(); return dict.keys(self)

    def values(self):
        self._all(); return dict.values(self)

    def items(self):
        self._all(); return dict.items(self)

    def __reversed__(self):
        self._all(); return dict.__reversed__(self)

    def __eq__(self, o):
        self._all(); return dict.__eq__(self, o)

    def __ne__(self, o):
        self._all(); return dict.__ne__(self, o)

    __hash__ = None

    def __repr__(self):
        self._all(); return dict.__repr__(self)

    def __or__(self, o):
        self._all(); return dict.__or__(self, o)

    def __ror__(self, o):
        self._all(); return dict.__ror__(self, o)

    def __ior__(self, o):
        self._all(); return dict.__ior__(self, o)

    def copy(self):
        self._all(); return dict(dict.items(self))

    def __reduce_ex__(self, proto):
        self._all(); return (dict, (dict(dict.items(self)),))

    def __copy__(self):
        self._all(); return dict(dict.items(self))

    def __deepcopy__(self, memo):
        self._all()
        import copy
        return copy.deepcopy(dict(dict.items(self)), memo)

    def setdefault(self, k, d=None):
        self._all(); return dict.setdefault(self, k, d)

    def pop(self, *a):
        self._all(); return dict.pop(self, *a)

    def popitem(self):
        self._all(); return dict.popitem(self)

    def update(self, *a, **k):
        self._all(); return dict.update(self, *a, **k)

    def clear(self):
        self._all(); return dict.clear(self)

    def __setitem__(self, k, v):
        self._all(); return dict.__setitem__(self, k, v)

    def __delitem__(self, k):
        self._all(); return dict.__delitem__(self, k)


def make_tdict(d, name):
    t = TDict(d)
    t._n = name
    return t
