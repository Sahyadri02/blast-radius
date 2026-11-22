"""Fingerprint of everything sqlglot builds while it is imported.

Run as: python fp.py <root>   (root holds the sqlglot/ package)
Prints one JSON object {module: digest} on stdout.

Deterministic across processes: no ids or addresses, sets sorted, functions by
qualified name plus their defaults and closure contents, sqlglot expressions by
their full repr. Function bodies are not hashed: tests that run a changed body are
found through line coverage; this only answers "did import-time state change?".
"""
import hashlib
import importlib
import json
import pkgutil
import sys
import types
from enum import Enum

_SKIP_CLASS_ATTRS = {"__dict__", "__weakref__", "__module__", "__doc__", "__annotations__",
                     "__firstlineno__", "__static_attributes__", "__orig_bases__", "__parameters__"}


def _feed(h, obj, depth, stack):
    u = h.update
    if depth > 40:
        u(b"<deep>")
        return
    t = type(obj)
    if obj is None or t in (bool, int, float, complex, str, bytes):
        u(t.__name__.encode()); u(repr(obj).encode()); u(b"\0")
        return
    if isinstance(obj, Enum):
        u(b"E"); u(t.__module__.encode()); u(t.__qualname__.encode()); u(obj.name.encode())
        u(repr(obj.value).encode() if isinstance(obj.value, (int, str, float, bool, type(None))) else b"")
        u(b"\0")
        return
    if isinstance(obj, (str, int, float, bytes)):  # subclasses of builtins
        u(b"B"); u(t.__qualname__.encode()); u(repr(obj).encode()); u(b"\0")
        return
    if isinstance(obj, type):
        u(b"C"); u((obj.__module__ or "").encode()); u(b"."); u(obj.__qualname__.encode()); u(b"\0")
        return
    if isinstance(obj, types.ModuleType):
        u(b"M"); u(obj.__name__.encode()); u(b"\0")
        return
    oid = id(obj)
    if oid in stack:
        u(b"<cycle>")
        return
    stack.add(oid)
    try:
        if isinstance(obj, (types.FunctionType, types.BuiltinFunctionType, types.MethodType)):
            f = getattr(obj, "__func__", obj)
            u(b"F"); u((getattr(f, "__module__", "") or "").encode()); u(b".")
            u((getattr(f, "__qualname__", "") or getattr(f, "__name__", "")).encode())
            if isinstance(f, types.FunctionType):
                if f.__defaults__:
                    u(b"D"); _feed(h, f.__defaults__, depth + 1, stack)
                if f.__kwdefaults__:
                    u(b"K"); _feed(h, f.__kwdefaults__, depth + 1, stack)
                if f.__closure__:
                    u(b"L")
                    for c in f.__closure__:
                        try:
                            _feed(h, c.cell_contents, depth + 1, stack)
                        except ValueError:
                            u(b"<empty>")
                # constants baked into the function's own code that come from import
                # time are its defaults and closures (above); its body is runtime code
            if hasattr(obj, "__self__") and not isinstance(obj.__self__, types.ModuleType):
                u(b"S"); _feed(h, obj.__self__, depth + 1, stack)
            u(b"\0")
            return
        if isinstance(obj, (classmethod, staticmethod)):
            u(b"W"); _feed(h, obj.__func__, depth + 1, stack)
            return
        if isinstance(obj, property):
            u(b"P")
            for g in (obj.fget, obj.fset, obj.fdel):
                _feed(h, g, depth + 1, stack)
            return
        if isinstance(obj, dict):
            # entry order is not hashed: some tables are built by iterating sets of
            # classes, whose order depends on memory addresses
            parts = []
            for k, v in obj.items():
                hh = hashlib.blake2b(digest_size=16)
                _feed(hh, k, depth + 1, stack); hh.update(b":"); _feed(hh, v, depth + 1, stack)
                parts.append(hh.digest())
            u(b"{"); u(t.__qualname__.encode())
            for d_ in sorted(parts):
                u(d_)
            u(b"}")
            return
        if isinstance(obj, (list, tuple)) and obj and all(isinstance(x, type) for x in obj):
            # sequences of classes are often built from sets (isinstance targets)
            u(b"T"); u(t.__qualname__.encode())
            for n in sorted(x.__module__ + "." + x.__qualname__ for x in obj):
                u(n.encode()); u(b",")
            u(b";")
            return
        if isinstance(obj, (list, tuple)):
            u(b"[" if isinstance(obj, list) else b"(")
            u(t.__qualname__.encode())
            for x in obj:
                _feed(h, x, depth + 1, stack); u(b",")
            u(b"]")
            return
        if isinstance(obj, (set, frozenset)):
            parts = []
            for x in obj:
                hh = hashlib.blake2b(digest_size=16)
                _feed(hh, x, depth + 1, stack)
                parts.append(hh.digest())
            u(b"S"); u(t.__name__.encode())
            for d in sorted(parts):
                u(d)
            u(b";")
            return
        # sqlglot expressions: full tree repr (slots-based objects)
        mod = getattr(t, "__module__", "") or ""
        if mod.startswith("sqlglot.expressions") and isinstance(getattr(obj, "args", None), dict):
            u(b"X"); u(t.__qualname__.encode())
            for k in sorted(obj.args):
                u(k.encode()); u(b":"); _feed(h, obj.args[k], depth + 1, stack); u(b";")
            for s in ("comments", "_type", "_meta"):
                if hasattr(obj, s):
                    u(s.encode()); u(b"="); _feed(h, getattr(obj, s), depth + 1, stack); u(b";")
            u(b"\0")
            return
        if isinstance(obj, (types.GeneratorType, types.CoroutineType, types.AsyncGeneratorType)):
            u(b"<lazy>"); u(t.__qualname__.encode())
            return
        if hasattr(obj, "pattern") and hasattr(obj, "flags") and t.__name__ == "Pattern":
            u(b"R"); _feed(h, obj.pattern, depth + 1, stack); u(repr(obj.flags).encode())
            return
        # functools.partial and similar
        if hasattr(obj, "func") and hasattr(obj, "args") and hasattr(obj, "keywords"):
            u(b"Q"); _feed(h, obj.func, depth + 1, stack); _feed(h, obj.args, depth + 1, stack)
            _feed(h, obj.keywords, depth + 1, stack)
            return
        # generic instance: its attributes (dict and slots)
        u(b"I"); u(mod.encode()); u(t.__qualname__.encode())
        state = {}
        d = getattr(obj, "__dict__", None)
        if isinstance(d, dict):
            state.update(d)
        for klass in t.__mro__:
            for s in getattr(klass, "__slots__", ()) or ():
                if isinstance(s, str) and s not in state and hasattr(obj, s):
                    try:
                        state[s] = getattr(obj, s)
                    except Exception:
                        pass
        for k in sorted(state):
            u(k.encode()); u(b"="); _feed(h, state[k], depth + 1, stack); u(b";")
        if not state:
            r = repr(obj)
            if " at 0x" not in r:
                u(r.encode())
        u(b"\0")
    finally:
        stack.discard(oid)


def fingerprint():
    import os
    import sqlglot
    if not os.path.abspath(sqlglot.__file__).startswith(os.path.abspath(sys.path[0])):
        raise SystemExit("wrong sqlglot imported")
    for m in pkgutil.walk_packages(sqlglot.__path__, "sqlglot."):
        if m.name.endswith("__main__"):
            continue
        try:
            importlib.import_module(m.name)
        except Exception as e:
            print("import-failed", m.name, repr(e), file=sys.stderr)
            raise
    out = {}
    for name in sorted(n for n in sys.modules if n == "sqlglot" or n.startswith("sqlglot.")):
        mod = sys.modules[name]
        if mod is None:
            continue
        h = hashlib.blake2b(digest_size=16)
        g = vars(mod)
        for k in sorted(g):
            if k.startswith("__") and k not in ("__all__",):
                continue
            v = g[k]
            if isinstance(v, type) and getattr(v, "__module__", None) == name:
                h.update(b"class:"); h.update(k.encode())
                _class(h, v, 0)
            else:
                h.update(k.encode()); h.update(b"="); _feed(h, v, 0, set()); h.update(b";")
        out[name] = h.hexdigest()
    return out


def _class(h, cls, depth):
    if depth > 6:
        return
    h.update(b"meta:"); h.update(type(cls).__qualname__.encode())
    h.update(b"mro:"); h.update(",".join(c.__module__ + "." + c.__qualname__ for c in cls.__mro__).encode())
    for k, v in list(vars(cls).items()):
        if k in _SKIP_CLASS_ATTRS:
            continue
        h.update(b"@"); h.update(k.encode()); h.update(b":")
        if isinstance(v, type) and v.__qualname__.startswith(cls.__qualname__ + "."):
            _class(h, v, depth + 1)  # nested class defined here
        else:
            _feed(h, v, 0, set())
        h.update(b";")


def detail(with_body=True):
    """Per-table view of import-time state:
    {"c": {table: {key: digest}}, "e": {set: ["#set"|"#frozenset", element keys...]},
     "f": {function: [core digest, defaults digest, file, body lines]},
     "s": {class: {ATTR: [digest, definer]}}, "a": {name: digest}}.
    Tables are exact dicts with nameable keys; "e" holds sets whose elements all have a
    stable key; "s" holds the scalar UPPER_CASE settings every Dialect/Tokenizer/Parser/
    Generator class resolves (inherited ones included); everything else is one digest per name."""
    import os
    import sqlglot
    if not os.path.abspath(sqlglot.__file__).startswith(os.path.abspath(sys.path[0])):
        raise SystemExit("wrong sqlglot imported")
    for m in pkgutil.walk_packages(sqlglot.__path__, "sqlglot."):
        if m.name.endswith("__main__"):
            continue
        importlib.import_module(m.name)
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from brtrack import walk_tables, krepr, set_keys, named_classes, family_classes, \
        resolved_scalars, is_scalar, _UPPER
    mods = {n: sys.modules[n] for n in sys.modules if n == "sqlglot" or n.startswith("sqlglot.")}
    c, a, e, f = {}, {}, {}, {}
    root_dir = os.path.dirname(os.path.dirname(os.path.abspath(sqlglot.__file__)))
    names = named_classes(mods)
    fam = {id(k) for k in family_classes(names)}
    seen_cls = set()
    memo = {}  # id -> (obj, digest): tables share most of their values across dialects
    ememo = {}

    def dig(x):
        m = memo.get(id(x))
        if m is not None and m[0] is x:
            return m[1]
        h = hashlib.blake2b(digest_size=8)
        _feed(h, x, 0, set())
        d = h.hexdigest()
        memo[id(x)] = (x, d)
        return d
    for name, owner, attr, v, is_class in walk_tables(mods):
        if is_class and id(owner) not in seen_cls:
            seen_cls.add(id(owner))
            h = hashlib.blake2b(digest_size=8)
            h.update(type(owner).__qualname__.encode())
            h.update(",".join(x.__module__ + "." + x.__qualname__ for x in owner.__mro__).encode())
            a[name.rsplit(".", 1)[0] + "#meta"] = h.hexdigest()
        if type(v) is dict:
            ks = [krepr(k) for k in v]
            if None not in ks and len(set(ks)) == len(ks):
                ent = {}
                for kr, x in zip(ks, v.values()):
                    ent[kr] = dig(x)
                c[name] = ent
                continue
        if type(v) in (set, frozenset) and not attr.startswith("__"):
            m = ememo.get(id(v))
            if m is None or m[0] is not v:
                m = ememo[id(v)] = (v, set_keys(v))
            if m[1] is not None:
                e[name] = ["#" + type(v).__name__] + m[1]
                continue
        if is_class and id(owner) in fam and _UPPER.match(attr) and is_scalar(v):
            continue  # in "s", resolved per class
        fe = _func_entry(v, attr, dig, root_dir, with_body)
        if fe is not None:
            f[name] = fe
            continue
        a[name] = dig(v)
    s = resolved_scalars(names)
    return {"c": c, "a": a, "e": e, "s": s, "f": f}


def _func_entry(v, attr, dig, root_dir, with_body=True):
    """[core digest, defaults digest, file, body lines] for a plain function (or a
    classmethod/staticmethod around one) defined in sqlglot; None for anything else.
    The core digest is everything fp hashes for a function except its defaults (kind,
    module, qualname, closure contents), so a change in the defaults alone is visible as
    such: it can only matter to calls, and every call runs a body line."""
    import os
    kind = type(v).__name__
    fn = v.__func__ if isinstance(v, (classmethod, staticmethod)) else v
    if type(fn) is not types.FunctionType or attr.startswith("__"):
        return None
    co = fn.__code__
    path = os.path.abspath(co.co_filename)
    if not path.startswith(root_dir + os.sep):
        return None
    rel = os.path.relpath(path, root_dir)
    h = hashlib.blake2b(digest_size=8)
    h.update(kind.encode()); h.update(b"|")
    h.update((fn.__module__ or "").encode()); h.update(b"."); h.update(fn.__qualname__.encode())
    if fn.__closure__:
        h.update(b"L")
        for cell in fn.__closure__:
            try:
                _feed(h, cell.cell_contents, 1, set())
            except ValueError:
                h.update(b"<empty>")
    dd, kd = fn.__defaults__, fn.__kwdefaults__
    dflt = ("d" + dig(dd) if dd is not None else "-") + ("k" + dig(kd) if kd is not None else "-")
    if not with_body:
        return [h.hexdigest(), dflt, rel]
    # body lines: every line of the code object after the def/decorator line
    lines = sorted({ln for _, _, ln in co.co_lines() if ln})
    body = [ln for ln in lines if ln > co.co_firstlineno]
    return [h.hexdigest(), dflt, rel, body]


if __name__ == "__main__":
    if sys.argv[1] == "--detail-diff":
        # python fp.py --detail-diff BASE.json ROOT: only what differs from BASE, as
        # {section: {name: value, or null when ROOT has no such name}}
        base_path, root = sys.argv[2], sys.argv[3]
        sys.path.insert(0, root)
        import io
        real = sys.stdout
        sys.stdout = io.StringIO()
        try:
            fp = detail(with_body=False)  # body lines come from the base file
        finally:
            sys.stdout = real
        with open(base_path) as fh:
            base = json.load(fh)
        out = {}
        for sec in set(base) | set(fp):
            b, g = base.get(sec, {}), fp.get(sec, {})
            if sec == "f":
                out[sec] = {n: g.get(n) for n in set(b) | set(g)
                            if (b.get(n) or [None])[:3] != (g.get(n) or [None])[:3]}
            else:
                out[sec] = {n: g.get(n) for n in set(b) | set(g) if b.get(n) != g.get(n)}
        print(json.dumps(out, sort_keys=True))
        raise SystemExit(0)
    if sys.argv[1] == "--detail":
        root = sys.argv[2]
        sys.path.insert(0, root)
        import io
        real = sys.stdout
        sys.stdout = io.StringIO()
        try:
            fp = detail()
        finally:
            sys.stdout = real
        print(json.dumps(fp, sort_keys=True))
        raise SystemExit(0)
    root = sys.argv[1]
    sys.path.insert(0, root)
    import io
    real = sys.stdout
    sys.stdout = io.StringIO()  # nothing sqlglot prints may reach our output
    try:
        fp = fingerprint()
    finally:
        sys.stdout = real
    print(json.dumps(fp))
