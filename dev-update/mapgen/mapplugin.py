import os, sys, json, importlib, pkgutil
import coverage

ROOT = os.path.abspath(os.getcwd())
OUT = os.environ["MAP_OUT"]
_cov = None
_cur = [""]
_reads = {}

def _audit(event, args):
    if event == "open" and args and isinstance(args[0], (str, bytes, os.PathLike)):
        try:
            p = os.fsdecode(args[0])
        except Exception:
            return
        p = os.path.abspath(p)
        if p.startswith(ROOT + os.sep):
            _reads.setdefault(_cur[0], set()).add(os.path.relpath(p, ROOT))

import pickle
from concurrent.futures import Future
import concurrent.futures as _cf

class InProcessPool:
    """Stand-in for ProcessPoolExecutor so coverage sees the work; pickles like the real one."""
    def __init__(self, max_workers=None, mp_context=None, initializer=None, initargs=(), **kw):
        if initializer:
            initializer(*pickle.loads(pickle.dumps(initargs)))
    def submit(self, fn, *a, **k):
        f = Future()
        try:
            a, k = pickle.loads(pickle.dumps((a, k)))
            f.set_result(pickle.loads(pickle.dumps(fn(*a, **k))))
        except BaseException as e:
            f.set_exception(e)
        return f
    def map(self, fn, *its, **kw):
        return [self.submit(fn, *x).result() for x in zip(*its)]
    def shutdown(self, *a, **k):
        pass
    def __enter__(self):
        return self
    def __exit__(self, *a):
        pass

def pytest_configure(config):
    global _cov
    _cf.ProcessPoolExecutor = InProcessPool
    import concurrent.futures.process as _cfp
    _cfp.ProcessPoolExecutor = InProcessPool
    _cov = coverage.Coverage(data_file=OUT + ".cov", source=[os.path.join(ROOT, "sqlglot")], branch=False)
    _cov.start()
    _cov.switch_context("IMPORT")
    sys.addaudithook(_audit)
    import sqlglot
    for m in pkgutil.walk_packages(sqlglot.__path__, "sqlglot."):
        try:
            importlib.import_module(m.name)
        except Exception as e:
            print("import fail", m.name, e, file=sys.stderr)
    _install_tracking()
    _install_sets_scalars()
    _setctx("WARMUP")
    _warmup()
    _wrap_inits()
    _setctx("")

_classes = {}

sys.path.insert(0, "/home/claude/entry")
import brtrack
_ACC = {}
_TRACK = {"on": False, "canon": {}, "alias": {}, "leaky": [], "derived": {}}
_ACC2 = {}
_T2 = {"ealias": {}, "eleaky": set(), "esets": [], "vdefs": [], "vleaky": set(), "scalars": []}

def _install_sets_scalars():
    """Tracked sets (per-element membership) and scalar UPPER_CASE settings of the
    Dialect/Tokenizer/Parser/Generator classes."""
    import gc
    mods = {n: m for n, m in sys.modules.items() if n == "sqlglot" or n.startswith("sqlglot.")}
    entries = list(brtrack.walk_tables(mods))
    canon, newobj, origs = {}, {}, {}
    while entries:
        name, owner, attr, v, is_class = entries.pop(0)
        if brtrack.set_keys(v) is None:
            continue
        i = id(v)
        if i not in newobj:
            canon[i] = name
            newobj[i] = brtrack.make_tset(v, name)
            origs[i] = v
        _T2["ealias"][name] = canon[i]
        if is_class:
            type.__setattr__(owner, attr, newobj[i])
        else:
            setattr(owner, attr, newobj[i])
        _T2["esets"].append((owner, attr, newobj[i], canon[i]))
    v = None
    gc.collect()
    holder = list(origs.values())
    skip = {id(origs), id(holder)} | {id(p) for p in newobj.values()}
    for i in list(origs):
        for r in gc.get_referrers(origs[i]):
            if id(r) in skip or type(r).__name__ == "frame":
                continue
            _T2["eleaky"].add(canon[i])
            if os.environ.get("LEAKDBG"):
                print("SETLEAK", canon[i], type(r).__name__, repr(r)[:100], file=sys.stderr)
            break
    del holder
    names = brtrack.named_classes(mods)
    cache = brtrack.ClassKeys(names)
    for c in brtrack.family_classes(names):
        for x, val in list(c.__dict__.items()):
            if brtrack._UPPER.match(x) and brtrack.is_scalar(val):
                d = brtrack.TScalar(val, names[c], x, cache)
                type.__setattr__(c, x, d)
                _T2["vdefs"].append(names[c] + "." + x)
                _T2["scalars"].append((c, x, d))

def _check_replaced():
    """A tracked object that is no longer in place (rebound or changed while tests ran)
    can't be trusted: later tests may depend on what an earlier test did to it."""
    for owner, attr, p, cn in _T2["esets"]:
        if (vars(owner).get(attr) if not isinstance(owner, type) else owner.__dict__.get(attr)) is not p:
            _T2["eleaky"].add(cn)
    for c, x, d in _T2["scalars"]:
        if c.__dict__.get(x) is not d:
            _T2["vleaky"].add(d.d + "." + x)

def _install_tracking():
    import gc
    mods = {n: m for n, m in sys.modules.items() if n == "sqlglot" or n.startswith("sqlglot.")}
    canon = {}      # id(orig) -> canonical name
    newobj = {}     # id(orig) -> TDict
    origs = {}
    alias = _TRACK["alias"]
    entries = list(brtrack.walk_tables(mods))
    while entries:
        name, owner, attr, v, is_class = entries.pop(0)
        if type(v) is not dict:
            continue
        ks = [brtrack.krepr(k) for k in v]
        if None in ks or len(set(ks)) != len(ks):
            continue
        i = id(v)
        if i not in newobj:
            canon[i] = name
            newobj[i] = brtrack.make_tdict(v, name)
            origs[i] = v
        alias[name] = canon[i]
        if is_class:
            type.__setattr__(owner, attr, newobj[i])
        else:
            setattr(owner, attr, newobj[i])
    v = None
    gc.collect()
    holder = list(origs.values())
    skip = {id(origs), id(holder)}
    for i in list(origs):
        for r in gc.get_referrers(origs[i]):
            if id(r) in skip or type(r).__name__ == "frame":
                continue
            _TRACK["leaky"].append(canon[i])
            if os.environ.get("LEAKDBG"):
                print("LEAK", canon[i], type(r).__name__, (list(r.keys())[:4] if isinstance(r, dict) else repr(r)[:100]), file=sys.stderr)
            break
    del holder
    _TRACK["canon"] = {id(t): t._n for t in newobj.values()}
    # generator dispatch tables are built from TRANSFORMS once per class and cached
    import sqlglot.generator as G
    orig_build = G._build_dispatch
    def _build_dispatch(cls):
        saved = brtrack.REC[0]
        brtrack.REC[0] = None
        try:
            d = orig_build(cls)
        finally:
            brtrack.REC[0] = saved
        n = "dispatch:" + cls.__module__ + ":" + cls.__qualname__
        src = _TRACK["canon"].get(id(cls.TRANSFORMS))
        _TRACK["derived"][n] = src
        return brtrack.make_tdict(d, n)
    G._build_dispatch = _build_dispatch
    cache = G._DISPATCH_CACHE
    for k in list(dict.keys(cache)):
        dict.__setitem__(cache, k, _build_dispatch(k))
    _TRACK["on"] = True

def _warmup():
    """Run each dialect's one-time setup (per-class caches) outside any test, so those
    lines count as setup lines that every user of the dialect depends on."""
    from sqlglot.dialects.dialect import Dialect
    try:
        classes = list(Dialect.classes.values()) if isinstance(getattr(Dialect, "classes", None), dict) else []
    except Exception:
        classes = []
    if not classes:
        from sqlglot.dialects.dialect import Dialects
        for d in Dialects:
            try:
                classes.append(type(Dialect.get_or_raise(d.value)))
            except Exception:
                pass
    classes.append(Dialect)
    for c in classes:
        try:
            d = c()
            _setctx("WARMUSE")
            d.tokenize("SELECT 1")
            _setctx("WARMUP")
            d.parser()
            d.generator()
        except Exception as e:
            print("warmup fail", c, e, file=sys.stderr)

def _wrap_inits():
    from sqlglot.expressions.core import Expr
    from sqlglot.dialects.dialect import Dialect
    from sqlglot.tokens import Tokenizer
    from sqlglot.parser import Parser
    from sqlglot.generator import Generator
    seen = set(); todo = [Expr, Dialect, Tokenizer, Parser, Generator]; allc = []
    while todo:
        c = todo.pop()
        if c in seen: continue
        seen.add(c); allc.append(c); todo.extend(c.__subclasses__())
    for c in allc:
        if "__init__" in c.__dict__:
            orig = c.__dict__["__init__"]
            def make(orig):
                def __init__(self, *a, **k):
                    t = type(self)
                    s = _classes.setdefault(_cur[0], set())
                    if t not in s:
                        s.add(t)
                    return orig(self, *a, **k)
                __init__.__wrapped__ = orig
                return __init__
            c.__init__ = make(orig)

def pytest_runtest_protocol(item, nextitem):
    _setctx(item.nodeid)

import pytest

def _setctx(c):
    _cur[0] = c
    _cov.switch_context(c)
    if _TRACK["on"]:
        brtrack.REC[0] = _ACC.setdefault(c, set())
        brtrack.REC2[0] = _ACC2.setdefault(c, set())

@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_setup(item):
    _setctx("SETUP::" + item.nodeid.rsplit("::", 1)[0])
    yield
    _setctx(item.nodeid)

@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_teardown(item, nextitem):
    _setctx("SETUP::" + item.nodeid.rsplit("::", 1)[0])
    yield
    _setctx(item.nodeid)

@pytest.hookimpl(hookwrapper=True)
def pytest_make_collect_report(collector):
    path = getattr(collector, "path", None)
    if isinstance(collector, pytest.Module) and path is not None:
        _setctx("COLLECT::" + os.path.relpath(str(path), ROOT))
        yield
        _setctx("")
    else:
        yield

def pytest_runtest_logfinish(nodeid, location):
    _setctx("")
    _check_replaced()

def pytest_unconfigure(config):
    _cov.stop()
    _cov.save()
    json.dump({k: sorted(v) for k, v in _reads.items()}, open(OUT + ".reads.json", "w"))
    cls = {}
    for k, ts in _classes.items():
        names = set()
        for t in ts:
            for b in t.__mro__:
                names.add(b.__module__ + ":" + b.__qualname__)
        cls[k] = sorted(names)
    json.dump(cls, open(OUT + ".classes.json", "w"))
    brtrack.REC[0] = None
    brtrack.REC2[0] = None
    _check_replaced()
    json.dump({"acc": {c: sorted(map(list, st)) for c, st in _ACC2.items()},
               "ealias": _T2["ealias"], "eleaky": sorted(_T2["eleaky"] | brtrack.MUTATED),
               "vdefs": sorted(_T2["vdefs"]), "vleaky": sorted(_T2["vleaky"])},
              open(OUT + ".acc2.json", "w"))
    acc = {}
    for c, st in _ACC.items():
        lst = []
        for n, k in st:
            kr = k if k in ("*", "#len") else brtrack.krepr(k)
            lst.append([n, kr if kr is not None else "*"])
        acc[c] = lst
    json.dump({"acc": acc, "alias": _TRACK["alias"], "leaky": sorted(set(_TRACK["leaky"])),
               "derived": _TRACK["derived"]}, open(OUT + ".acc.json", "w"))
