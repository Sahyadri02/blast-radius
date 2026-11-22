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


# ---------------------------------------------------------------- sets and scalar settings
# Set elements are named by a key that two elements share exactly when they compare equal
# (and hash equal) as set members: int-likes (int, bool, IntEnum such as TokenType,
# integral floats) by integer value, strings (including str enums) by value, enum members
# without value equality, classes and functions by identity, tuples element-wise.
# Anything else has no key: its set is not tracked, and a membership probe with no key
# counts as a read of the whole set.
import re as _re

_UPPER = _re.compile(r"^[A-Z][A-Z0-9_]*$")


def ekey(x):
    t = type(x)
    if x is None:
        return "v:None"
    if t is str:
        return "s:" + x
    if isinstance(x, str):
        return "s:" + str.__str__(x) if t.__eq__ is str.__eq__ and t.__hash__ in (str.__hash__, Enum.__hash__) else None
    if isinstance(x, int):
        return "n:" + repr(int(x)) if t.__eq__ is int.__eq__ or t.__eq__ is bool.__eq__ else None
    if t is float:
        return "n:" + repr(int(x)) if x.is_integer() else "x:" + repr(x)
    if isinstance(x, Enum):
        return "e:" + t.__module__ + "." + t.__qualname__ + "." + x.name if t.__eq__ is object.__eq__ else None
    if isinstance(x, type):
        m = type(x)
        if m.__eq__ is object.__eq__ and m.__hash__ is object.__hash__:
            return "t:" + (x.__module__ or "") + "." + x.__qualname__
        return None
    if t is tuple:
        ks = [ekey(y) for y in x]
        return None if None in ks else "(" + ",".join(ks) + ")"
    if t is types.FunctionType:
        return "f:" + (x.__module__ or "") + "." + x.__qualname__ + "@" + str(x.__code__.co_firstlineno)
    return None


def set_keys(v):
    """Sorted element keys of a plain set/frozenset whose elements all have distinct keys
    (None when it cannot be tracked per element)."""
    if type(v) not in (set, frozenset):
        return None
    ks = [ekey(x) for x in v]
    if None in ks or len(set(ks)) != len(ks):
        return None
    return sorted(ks)


def is_scalar(v):
    return v is None or type(v) in (bool, int, float, str) or (isinstance(v, Enum) and
                                                              isinstance(v.value, (bool, int, float, str)))


def scalar_digest(v):
    t = type(v)
    return t.__module__ + "." + t.__qualname__ + ":" + repr(v)


def families():
    from sqlglot.dialects.dialect import Dialect
    from sqlglot.generator import Generator
    from sqlglot.parser import Parser
    from sqlglot import tokens
    return (Dialect, tokens._TokenizerBase, Parser, Generator)


class IdMap:
    """{class: value} keyed by identity: some sqlglot metaclasses define __hash__/__eq__
    (dialect classes compare equal to their names), and tracking must not run them."""
    __slots__ = ("d", "keep")

    def __init__(self):
        self.d, self.keep = {}, []

    def __contains__(self, c):
        return id(c) in self.d

    def __setitem__(self, c, v):
        if id(c) not in self.d:
            self.keep.append(c)
        self.d[id(c)] = v

    def __getitem__(self, c):
        return self.d[id(c)]

    def get(self, c, default=None):
        return self.d.get(id(c), default)

    def items(self):
        return [(c, self.d[id(c)]) for c in self.keep]

    def __iter__(self):
        return iter(list(self.keep))


def named_classes(modules):
    """IdMap {class: key} for the classes walk_tables visits (key "<module>:<qualname>")
    plus the component classes a dialect builds on the fly (key "<dialect key>#<attr>")."""
    names = IdMap()
    order = []
    for mname in sorted(modules):
        mod = modules[mname]
        if mod is None:
            continue
        g = vars(mod)
        seen = set()
        for k in sorted(g):
            v = g[k]
            if isinstance(v, type) and getattr(v, "__module__", None) == mname:
                out = []
                _classes_of(v, mname, 0, out, seen)
                for c in out:
                    if c not in names:
                        names[c] = mname + ":" + c.__qualname__
                        order.append(c)
    fam = families()
    D = fam[0]
    for c in list(order):
        if issubclass(c, D):
            for attr in ("tokenizer_class", "jsonpath_tokenizer_class", "parser_class", "generator_class"):
                v = c.__dict__.get(attr)
                if isinstance(v, type) and v not in names:
                    names[v] = names[c] + "#" + attr
    return names


def family_classes(names):
    fam = families()
    return [c for c in names if issubclass(c, fam)]


def resolved_scalars(names):
    """{class key: {ATTR: [digest, definer key]}} for every UPPER_CASE attribute that a
    dialect-family class resolves to a scalar value (inherited ones included)."""
    out = {}
    for c in family_classes(names):
        ent = {}
        done = set()
        for b in c.__mro__:
            for x, v in b.__dict__.items():
                if x in done or not _UPPER.match(x):
                    continue
                done.add(x)
                if isinstance(v, TScalar):
                    v = v.v
                if is_scalar(v):
                    ent[x] = [scalar_digest(v), names.get(b, "?")]
        if ent:
            out[names[c]] = ent
    return out


REC2 = [None]  # the set/scalar access set of the current context (or None: not recording)
MUTATED = set()  # names of tracked sets changed in place while tests ran


class TScalar:
    """Stands in for a scalar class setting: reading it records the lookup class (and the
    class that defines it) as ("v:<class key>.<ATTR>", "v") / ("d:<definer>.<ATTR>", "v")."""
    __slots__ = ("v", "d", "x", "cache")

    def __init__(self, v, definer, attr, cache):
        self.v, self.d, self.x, self.cache = v, definer, attr, cache

    def __get__(self, obj, owner=None):
        s = REC2[0]
        if s is not None:
            T = type(obj) if obj is not None else owner
            ks = self.cache.get(T)
            if ks is None:
                ks = self.cache.lookup(T)
            for k in ks:
                s.add(("v:" + k + "." + self.x, "v"))
            s.add(("d:" + self.d + "." + self.x, "v"))
        return self.v


class ClassKeys:
    """class -> keys to record a lookup under: its own key when named, else the keys of
    every named class in its MRO (classes made while tests run). Keyed by identity."""
    __slots__ = ("d", "names", "keep")

    def __init__(self, names):
        self.names = names
        self.d = {id(c): (k,) for c, k in names.items()}
        self.keep = list(names)

    def get(self, T):
        return self.d.get(id(T))

    def lookup(self, T):
        ks = tuple(self.names.get(b) for b in getattr(T, "__mro__", ()) if b in self.names)
        self.d[id(T)] = ks
        self.keep.append(T)
        return ks


def _unwrap(o):
    if isinstance(o, _TSetBase):
        o._all()
        return o._s
    return o


class _TSetBase:
    """Proxy for a tracked set. Not a set subclass, so no C fast path (set algebra, set(),
    frozenset(), update(), PyAnySet checks) can read the elements without going through
    these methods; isinstance(x, set/frozenset) still holds through __class__."""
    __slots__ = ("_s", "_n")

    def _all(self):
        s = REC2[0]
        if s is not None:
            s.add((self._n, "*"))

    def __contains__(self, x):
        r = x in self._s
        s = REC2[0]
        if s is not None:
            k = ekey(x)
            s.add((self._n, k if k is not None else "*"))
        return r

    @property
    def __class__(self):
        return type(self._s)

    def __iter__(self):
        self._all(); return iter(self._s)

    def __len__(self):
        self._all(); return len(self._s)

    def __bool__(self):
        self._all(); return bool(self._s)

    def __repr__(self):
        self._all(); return repr(self._s)

    __str__ = __repr__

    def __eq__(self, o):
        self._all(); return self._s == _unwrap(o)

    def __ne__(self, o):
        self._all(); return self._s != _unwrap(o)

    def __lt__(self, o):
        self._all(); return self._s < _unwrap(o)

    def __le__(self, o):
        self._all(); return self._s <= _unwrap(o)

    def __gt__(self, o):
        self._all(); return self._s > _unwrap(o)

    def __ge__(self, o):
        self._all(); return self._s >= _unwrap(o)

    def __or__(self, o):
        self._all(); return self._s | _unwrap(o)

    def __ror__(self, o):
        self._all(); return _unwrap(o) | self._s

    def __and__(self, o):
        self._all(); return self._s & _unwrap(o)

    def __rand__(self, o):
        self._all(); return _unwrap(o) & self._s

    def __sub__(self, o):
        self._all(); return self._s - _unwrap(o)

    def __rsub__(self, o):
        self._all(); return _unwrap(o) - self._s

    def __xor__(self, o):
        self._all(); return self._s ^ _unwrap(o)

    def __rxor__(self, o):
        self._all(); return _unwrap(o) ^ self._s

    def copy(self):
        self._all(); return self._s.copy()

    def union(self, *a):
        self._all(); return self._s.union(*map(_unwrap, a))

    def intersection(self, *a):
        self._all(); return self._s.intersection(*map(_unwrap, a))

    def difference(self, *a):
        self._all(); return self._s.difference(*map(_unwrap, a))

    def symmetric_difference(self, o):
        self._all(); return self._s.symmetric_difference(_unwrap(o))

    def issubset(self, o):
        self._all(); return self._s.issubset(_unwrap(o))

    def issuperset(self, o):
        self._all(); return self._s.issuperset(_unwrap(o))

    def isdisjoint(self, o):
        self._all(); return self._s.isdisjoint(_unwrap(o))

    def __reduce_ex__(self, proto):
        self._all(); return (type(self._s), (list(self._s),))

    def __reduce__(self):
        self._all(); return (type(self._s), (list(self._s),))

    def __copy__(self):
        self._all(); return type(self._s)(self._s)

    def __deepcopy__(self, memo):
        self._all()
        import copy
        return copy.deepcopy(type(self._s)(self._s), memo)

    def __sizeof__(self):
        return self._s.__sizeof__()

    def __getattr__(self, a):
        # anything not listed above: a whole-set read, answered by the real set
        if a in ("_s", "_n"):
            raise AttributeError(a)
        self._all()
        return getattr(self._s, a)


class TFrozenSet(_TSetBase):
    __slots__ = ()

    def __hash__(self):
        self._all(); return hash(self._s)


class TSet(_TSetBase):
    __slots__ = ()
    __hash__ = None

    def _mut(self):
        self._all()
        MUTATED.add(self._n)

    def add(self, x):
        self._mut(); self._s.add(x)

    def discard(self, x):
        self._mut(); self._s.discard(x)

    def remove(self, x):
        self._mut(); self._s.remove(x)

    def pop(self):
        self._mut(); return self._s.pop()

    def clear(self):
        self._mut(); self._s.clear()

    def update(self, *a):
        self._mut(); self._s.update(*map(_unwrap, a))

    def intersection_update(self, *a):
        self._mut(); self._s.intersection_update(*map(_unwrap, a))

    def difference_update(self, *a):
        self._mut(); self._s.difference_update(*map(_unwrap, a))

    def symmetric_difference_update(self, o):
        self._mut(); self._s.symmetric_difference_update(_unwrap(o))

    def __ior__(self, o):
        self._mut(); self._s |= _unwrap(o); return self

    def __iand__(self, o):
        self._mut(); self._s &= _unwrap(o); return self

    def __isub__(self, o):
        self._mut(); self._s -= _unwrap(o); return self

    def __ixor__(self, o):
        self._mut(); self._s ^= _unwrap(o); return self


def make_tset(v, name):
    p = (TFrozenSet if type(v) is frozenset else TSet).__new__(TFrozenSet if type(v) is frozenset else TSet)
    object.__setattr__(p, "_s", v)
    object.__setattr__(p, "_n", name)
    return p
