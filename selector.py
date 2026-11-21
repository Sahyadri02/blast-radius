"""Blast Radius selector: pick tests whose recorded coverage touches the change.

Map (built from the pinned sqlglot commit): for every source line, which tests
executed it; which lines run at import time; which tests read which data files.
"""
import ast, gzip, io, json, os, re, sys, tokenize
from pathlib import Path

DATA = Path(os.environ.get("BR_DATA", "/data"))
APP = Path(__file__).resolve().parent
SRC = Path(os.environ.get("BR_SRC", "/opt/sqlglot"))
MAXW = 8
WORKER_MS = 2000

def log(*a):
    print(*a, file=sys.stderr)

def parse_patch(text):
    """-> {path: {"old": str|None, "new": str|None, "hunks": [(old_start, [(tag, line)])]}}"""
    files = {}
    cur = None
    lines = text.split("\n")
    i = 0
    while i < len(lines):
        l = lines[i]
        if l.startswith("diff --git "):
            cur = {"old": None, "new": None, "hunks": [], "binary": False}
            m = re.match(r"diff --git a/(.*) b/(.*)", l)
            cur["a"], cur["b"] = (m.group(1), m.group(2)) if m else (None, None)
            files[len(files)] = cur
        elif l.startswith("--- ") and cur is not None and not cur["hunks"]:
            p = l[4:].split("\t")[0]
            cur["old"] = None if p == "/dev/null" else p[2:] if p.startswith("a/") else p
        elif l.startswith("+++ ") and cur is not None and not cur["hunks"]:
            p = l[4:].split("\t")[0]
            cur["new"] = None if p == "/dev/null" else p[2:] if p.startswith("b/") else p
        elif l.startswith("Binary files") and cur is not None:
            cur["binary"] = True
        elif l.startswith("@@") and cur is not None:
            m = re.match(r"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@", l)
            ostart = int(m.group(1)); ocount = int(m.group(2) or 1)
            ncount = int(m.group(4) or 1)
            body = []
            i += 1
            oc = nc = 0
            while i < len(lines) and (oc < ocount or nc < ncount):
                b = lines[i]
                if b.startswith("\\"):
                    i += 1; continue
                tag = b[:1] if b else " "
                if tag == " ":
                    oc += 1; nc += 1
                elif tag == "-":
                    oc += 1
                elif tag == "+":
                    nc += 1
                else:
                    break
                body.append((tag, b[1:]))
                i += 1
            while i < len(lines) and lines[i].startswith("\\"):
                i += 1
            cur["hunks"].append((ostart, body))
            continue
        i += 1
    out = {}
    for f in files.values():
        path = f["new"] or f["old"] or f.get("b")
        out[path] = f
    return out

def apply(old_text, hunks):
    old = old_text.split("\n")
    new = []
    pos = 0  # index into old (0-based)
    for ostart, body in hunks:
        s = ostart - 1 if ostart > 0 else 0
        new.extend(old[pos:s]); pos = s
        for tag, line in body:
            if tag == " ":
                new.append(old[pos] if pos < len(old) else line); pos += 1
            elif tag == "-":
                pos += 1
            else:
                new.append(line)
    new.extend(old[pos:])
    return "\n".join(new)

def touched(hunks):
    """Old line numbers removed, plus old neighbours of insertions."""
    out = set()
    for ostart, body in hunks:
        o = ostart  # next old line number
        prev = ostart - 1
        for tag, line in body:
            if tag == " ":
                prev = o; o += 1
            elif tag == "-":
                out.add(o); prev = o; o += 1
            else:
                if prev >= 1: out.add(prev)
                out.add(o)
    return out

CLASS_BY_MODULE = {}
GROUP_DIRS = ("dialects", "parsers", "generators", "tokenizers", "typing")

def stmt_starts(text, lns):
    """All header lines of the innermost statement around each line (a changed operator
    can matter to any test that reached the statement)."""
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return set()
    spans = []
    for node in ast.walk(tree):
        if isinstance(node, ast.stmt):
            body_lines = set()
            for fld in ("body", "orelse", "finalbody", "handlers"):
                for ch in getattr(node, fld, []) or []:
                    body_lines.update(range(ch.lineno, (ch.end_lineno or ch.lineno) + 1))
            spans.append((node.lineno, node.end_lineno or node.lineno, body_lines))
    out = set()
    for ln in lns:
        best = None
        for s, e, body in spans:
            if s <= ln <= e and ln not in body and (best is None or s > best[0]):
                best = (s, e, body)
        if best is not None:
            s, e, body = best
            out.update(x for x in range(s, e + 1) if x not in body)
    return out

def stem_of(path):
    parts = path.split("/")
    if len(parts) == 3 and parts[0] == "sqlglot" and parts[1] in GROUP_DIRS and parts[2].endswith(".py"):
        stem = parts[2][:-3]
        if stem not in ("__init__", "dialect", "base"):
            return stem
    return None

_children = None
def children():
    global _children
    if _children is None:
        _children = {}
        for d in GROUP_DIRS:
            for f in (SRC / "sqlglot" / d).glob("*.py"):
                txt = f.read_text()
                for m in re.finditer(r"from sqlglot\.(?:dialects|parsers|generators|tokenizers|typing)\.(\w+) import|from sqlglot\.(?:dialects|parsers|generators|tokenizers|typing) import (\w+)", txt):
                    parent = m.group(1) or m.group(2)
                    if parent != f.stem:
                        _children.setdefault(parent, set()).add(f.stem)
    return _children

def descendants(stem):
    seen = {stem}; todo = [stem]
    ch = children()
    while todo:
        s = todo.pop()
        for c in ch.get(s, ()):
            if c not in seen:
                seen.add(c); todo.append(c)
    return seen

def try_lines(text, lns):
    """For changes inside a try statement (handlers, else, finally), every line of the
    try body: tests that entered the try can be affected."""
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return set()
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Try, getattr(ast, "TryStar", ast.Try))):
            s, e = node.lineno, node.end_lineno or node.lineno
            if any(s <= ln <= e for ln in lns):
                out.update(range(s, (node.body[-1].end_lineno or s) + 1))
    return out

_import_root = None
def import_ok(changed):
    """Import every changed module from a patched copy of the source. False on failure."""
    global _import_root
    import shutil, subprocess, tempfile
    try:
        if _import_root is None:
            _import_root = tempfile.mkdtemp(prefix="br_")
            for d, _, files in os.walk(SRC / "sqlglot"):
                rel = os.path.relpath(d, SRC)
                os.makedirs(os.path.join(_import_root, rel), exist_ok=True)
                for fn in files:
                    if fn.endswith(".py") or fn.endswith(".typed"):
                        shutil.copyfile(os.path.join(d, fn), os.path.join(_import_root, rel, fn))
        root = Path(_import_root)
        for path, text in changed.items():
            (root / path).write_text(text)
        mods = [p[:-3].replace("/", ".").removesuffix(".__init__") for p in changed]
        code = ("import sys; sys.path.insert(0, %r)\nimport importlib, sqlglot\n"
                "for m in %r: importlib.import_module(m)\n" % (str(root), mods))
        r = subprocess.run([sys.executable, "-B", "-c", code], capture_output=True,
                           timeout=60, cwd=str(root))
        ok = r.returncode == 0
    except Exception as e:
        log("import check error", repr(e)); ok = False
    finally:
        try:
            for path in changed:
                shutil.copyfile(SRC / path, Path(_import_root) / path)
        except Exception:
            _import_root = None
    return ok

def code_tokens(text):
    toks = []
    for t in tokenize.generate_tokens(io.StringIO(text).readline):
        if t.type in (tokenize.COMMENT, tokenize.NL, tokenize.ENCODING):
            continue
        toks.append((t.type, t.string))
    return toks

def main():
    tests = json.loads((DATA / "tests.json").read_text())["tests"]
    n = len(tests)
    ALL = (1 << n) - 1
    by_file = {}
    for t in tests:
        by_file[t["file"]] = by_file.get(t["file"], 0) | (1 << t["number"])
    with gzip.open(APP / "map.json.gz", "rt") as fh:
        M = json.load(fh)
    LINES = M["lines"]; IMP = {f: set(v) for f, v in M["import"].items()}
    READS = {p: int(m, 16) for p, m in M["reads"].items()}
    CLASSES = {c: int(m, 16) for c, m in M.get("classes", {}).items()}
    for c, m in CLASSES.items():
        CLASS_BY_MODULE.setdefault(c.split(":")[0] + ":", []).append((c, m))
    filemask_cache = {}
    def filemask(path):
        if path not in filemask_cache:
            m = 0
            for v in LINES.get(path, {}).values():
                m |= int(v, 16)
            filemask_cache[path] = m
        return filemask_cache[path]
    ms = [round(t["seconds"] * 1000) for t in tests]

    for patch in sorted((DATA / "commits").glob("*.patch")):
        cid = patch.stem
        try:
            sel = select(patch.read_text(errors="replace"), LINES, IMP, READS, by_file, filemask, ALL, CLASSES)
        except Exception as e:
            log(cid, "error", repr(e)); sel = ALL
        nums = [i for i in range(n) if sel >> i & 1]
        workers = split(nums, ms)
        print(json.dumps({"commit": cid, "workers": workers}))
        log(cid, len(nums), "tests", len(workers), "workers")

def select(text, LINES, IMP, READS, by_file, filemask, ALL, CLASSES=None):
    sel = 0
    import_checks = {}
    for path, f in parse_patch(text).items():
        # Some tests read source files as data (e.g. test_typing reads sqlglot/typing/*.py),
        # so a file's readers always count, even for comment-only edits.
        sel |= READS.get(path, 0) | READS.get(f.get("old") or "", 0)
        if f["binary"] or f["old"] is None or f["new"] is None or f["old"] != f["new"]:
            return ALL  # added/deleted/renamed/binary: be safe
        hunks = f["hunks"]
        if path.endswith(".py") and path.startswith("sqlglot/"):
            src = SRC / path
            if not src.exists():
                return ALL
            old_text = src.read_text()
            new_text = apply(old_text, hunks)
            try:
                compile(new_text, path, "exec")
                # bytes, like the import system: catches coding declarations
                compile(new_text.encode("utf-8"), path, "exec")
            except (SyntaxError, ValueError, UnicodeError):
                return ALL
            new_lines = [l for _, b in hunks for t, l in b if t == "+"]
            old_lines = [l for _, b in hunks for t, l in b if t == "-"]
            if any(re.match(r"^[ \t\f]*#.*?coding[:=]", l) for l in new_lines + old_lines):
                return ALL
            try:
                if code_tokens(old_text) == code_tokens(new_text):
                    continue  # comment / blank-line only
            except (tokenize.TokenError, IndentationError, SyntaxError):
                return ALL
            fl = LINES.get(path, {})
            imp = IMP.get(path, set())
            lns = touched(hunks)
            lns |= stmt_starts(old_text, lns)
            lns |= try_lines(old_text, lns)
            if any("_sql_names" in l for l in new_lines + old_lines):
                # function-name registry: tests that built anonymous functions may now
                # resolve to (or stop resolving to) a real class
                for key, mask in CLASSES.items():
                    if key.endswith((":Anonymous", ":AnonymousAggFunc")):
                        sel |= mask
            if any(ln in imp for ln in lns):
                import_checks[path] = new_text
            for ln in lns:
                if ln in imp:
                    sel |= import_fallback(path, filemask, by_file, ALL, old_text, ln, CLASSES)
                v = fl.get(str(ln))
                if v:
                    sel |= int(v, 16)
        elif path in READS:
            pass  # readers already added above
        elif path in by_file:
            sel |= by_file[path]
        else:
            return ALL
    if import_checks and sel != ALL and not import_ok(import_checks):
        return ALL
    return sel

def enclosing_classes(text, ln):
    """Qualified name of the innermost class whose body (outside any function) holds ln."""
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return None
    best = None
    def walk(node, prefix):
        nonlocal best
        for ch in ast.iter_child_nodes(node):
            if isinstance(ch, ast.ClassDef):
                s = min([ch.lineno] + [d.lineno for d in ch.decorator_list])
                if s <= ln <= (ch.end_lineno or ch.lineno):
                    q = prefix + ch.name
                    best = q
                    walk(ch, q + ".")
            elif isinstance(ch, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                continue
            else:
                walk(ch, prefix)
    walk(tree, "")
    return best

def import_fallback(path, filemask, by_file, ALL, text=None, ln=None, CLASSES=None):
    stem = stem_of(path)
    if stem is None:
        if text is not None and CLASSES:
            q = enclosing_classes(text, ln)
            if q:
                key = path[:-3].replace("/", ".") + ":" + q
                if key in CLASSES:
                    return CLASSES[key] | filemask(path)
        return ALL
    m = 0
    for s in descendants(stem):
        for d in GROUP_DIRS:
            m |= filemask(f"sqlglot/{d}/{s}.py")
        m |= by_file.get(f"tests/dialects/test_{s}.py", 0)
        # tests that instantiated any class of this dialect family (e.g. tests that loop
        # over every dialect and only read its settings)
        for d in GROUP_DIRS:
            pre = f"sqlglot.{d}.{s}:"
            for key, mask in CLASS_BY_MODULE.get(pre, ()):
                m |= mask
    return m

def split(nums, ms):
    if not nums:
        return []
    order = sorted(nums, key=lambda i: -ms[i])
    total = sum(ms[i] for i in nums)
    best = None
    for k in range(1, min(MAXW, len(nums)) + 1):
        loads = [0] * k; ws = [[] for _ in range(k)]
        for i in order:
            j = loads.index(min(loads)); ws[j].append(i); loads[j] += ms[i]
        cost = max(loads) + WORKER_MS * k
        if best is None or cost < best[0]:
            best = (cost, ws)
    return [w for w in best[1] if w]

if __name__ == "__main__":
    main()
