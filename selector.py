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
            cur.setdefault("nstarts", []).append(int(m.group(3)))
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
NARROW = [None, None]
TESTIMP = {}
WIDE = [0]
GROUP_DIRS = ("dialects", "parsers", "generators", "tokenizers", "typing")

_ast_cache = {}
def parse_cached(text):
    key = hash(text)
    if key not in _ast_cache:
        if len(_ast_cache) > 64:
            _ast_cache.clear()
        try:
            tree = ast.parse(text)
        except SyntaxError:
            tree = None
        _ast_cache[key] = (tree, None)
    return _ast_cache[key][0]

def stmt_spans(text):
    key = hash(text)
    tree = parse_cached(text)
    if tree is None:
        return None
    if _ast_cache[key][1] is None:
        spans = []
        for node in ast.walk(tree):
            if isinstance(node, ast.stmt):
                body = []
                for fld in ("body", "orelse", "finalbody", "handlers"):
                    for ch in getattr(node, fld, []) or []:
                        body.append((ch.lineno, ch.end_lineno or ch.lineno))
                spans.append((node.lineno, node.end_lineno or node.lineno, body))
        _ast_cache[key] = (tree, spans)
    return _ast_cache[key][1]

def stmt_starts(text, lns):
    """All header lines of the innermost statement around each line (a changed operator
    can matter to any test that reached the statement)."""
    spans = stmt_spans(text)
    if spans is None:
        return set()
    inbody = lambda x, body: any(a <= x <= b for a, b in body)
    out = set()
    for ln in lns:
        best = None
        for s, e, body in spans:
            if s <= ln <= e and (best is None or s > best[0]) and not inbody(ln, body):
                best = (s, e, body)
        if best is not None:
            s, e, body = best
            out.update(x for x in range(s, e + 1) if not inbody(x, body))
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

def added_new_lines(f):
    out = []
    for (ostart, body), nstart in zip(f["hunks"], f.get("nstarts", [])):
        n = nstart
        for tag, line in body:
            if tag == "+":
                if line.strip() and not line.strip().startswith("#"):
                    out.append(n)
                n += 1
            elif tag == " ":
                n += 1
    return out

def outside_functions(text, lns):
    """Lines (of text) that run at import time, i.e. outside any function body.
    Returns {line: enclosing class qualname or ''}."""
    tree = ast.parse(text)
    res = {}
    lns = set(lns)
    def mark(a, b, prefix):
        for ln in lns:
            if a <= ln <= b:
                res.setdefault(ln, prefix[:-1])
    def walk(body, prefix):
        for ch in body:
            s = ch.lineno; e = ch.end_lineno or s
            if isinstance(ch, ast.ClassDef):
                top = min([s] + [d.lineno for d in ch.decorator_list])
                mark(top, max(s, ch.body[0].lineno - 1), prefix + ch.name + ".")
                walk(ch.body, prefix + ch.name + ".")
            elif isinstance(ch, (ast.FunctionDef, ast.AsyncFunctionDef)):
                top = min([s] + [d.lineno for d in ch.decorator_list])
                f0 = ch.body[0]
                b0 = min([f0.lineno] + [d.lineno for d in getattr(f0, "decorator_list", [])])
                mark(top, b0 - 1 if b0 > s else s, prefix)  # decorators, signature, defaults
            elif isinstance(ch, ast.If) and "TYPE_CHECKING" in ast.dump(ch.test) and not ch.orelse:
                continue  # never runs
            elif isinstance(ch, (ast.If, ast.For, ast.While, ast.With, ast.Try)):
                first = min([x.lineno for x in getattr(ch, "body", [])] or [s])
                mark(s, max(s, first - 1), prefix)  # the header itself
                for fld in ("body", "orelse", "finalbody"):
                    walk(getattr(ch, fld, []) or [], prefix)
                for h in getattr(ch, "handlers", []) or []:
                    mark(h.lineno, max(h.lineno, h.body[0].lineno - 1), prefix)
                    walk(h.body, prefix)
            else:
                mark(s, e, prefix)
    walk(tree.body, "")
    return res

def chain_heads(text, lns):
    """For an if/elif header, the header lines of every earlier link of its chain:
    turning `elif` into `if` (or editing a condition) matters to every test that
    evaluated the first condition."""
    tree = parse_cached(text)
    if tree is None:
        return set()
    out = set()
    def hdr(n):
        return set(range(n.lineno, n.body[0].lineno)) or {n.lineno}
    def walk(node, parents):
        for ch in ast.iter_child_nodes(node):
            if isinstance(ch, ast.If):
                h = hdr(ch)
                if h & lns:
                    for par in parents:
                        out.update(hdr(par))
                chain = parents + [ch] if ch.orelse and len(ch.orelse) == 1 and isinstance(ch.orelse[0], ast.If) else []
                for b in ch.body:
                    walk_one(b, [])
                for b in ch.orelse:
                    if isinstance(b, ast.If) and chain:
                        walk_one(b, chain)
                    else:
                        walk_one(b, [])
            else:
                walk(ch, [])
    def walk_one(n, parents):
        if isinstance(n, ast.If):
            wrapper = ast.Module(body=[n], type_ignores=[])
            walk(wrapper, parents)
        else:
            walk(n, [])
            if isinstance(n, ast.stmt) and not isinstance(n, ast.If):
                pass
    walk(tree, [])
    return out

def top_names(text):
    tree = parse_cached(text) if text is not None else None
    if tree is None:
        return None
    names = set()
    def stmts(body):
        for n in body:
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                names.add(n.name)
            elif isinstance(n, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
                for t in (n.targets if isinstance(n, ast.Assign) else [n.target]):
                    for x in ast.walk(t):
                        if isinstance(x, ast.Name) and isinstance(x.ctx, ast.Store):
                            names.add(x.id)
            elif isinstance(n, (ast.Import, ast.ImportFrom)):
                for a in n.names:
                    names.add((a.asname or a.name).split(".")[0])
            elif isinstance(n, ast.If):
                if "TYPE_CHECKING" in ast.dump(n.test):
                    stmts(n.orelse)  # the body never runs
                    continue
                stmts(n.body); stmts(n.orelse)
            elif isinstance(n, ast.Delete):
                for t in n.targets:
                    if isinstance(t, ast.Name):
                        names.discard(t.id)
            elif isinstance(n, (ast.Try, getattr(ast, "TryStar", ast.Try))):
                stmts(n.body); stmts(n.orelse); stmts(n.finalbody)
                for h in n.handlers:
                    stmts(h.body)
            elif isinstance(n, (ast.With, ast.For, ast.While)):
                stmts(n.body)
    stmts(tree.body)
    for n in tree.body:
        if isinstance(n, ast.Delete):
            for t in n.targets:
                if isinstance(t, ast.Name):
                    names.discard(t.id)
    return names

def test_files_closure(files):
    seen = set(files); todo = list(files)
    while todo:
        f = todo.pop()
        for g in TESTIMP.get("testdeps", {}).get(f, ()):
            if g not in seen:
                seen.add(g); todo.append(g)
    return seen

def in_function(text, ln):
    tree = parse_cached(text)
    if tree is None:
        return True
    for n in ast.walk(tree):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)) and n.body:
            b = n.body if isinstance(n.body, list) else [n.body]
            if b[0].lineno <= ln <= (n.end_lineno or ln):
                return True
    return False

def registry_owner_mask(lines, CLASSES):
    """Classes currently registered under the function names an edit mentions."""
    m = 0
    try:
        ensure_sqlglot()
        from sqlglot import expressions as E
        reg = getattr(E, "FUNCTION_BY_NAME", {})
        for l in lines:
            for name in re.findall(r"[\"']([A-Za-z_][A-Za-z0-9_]*)[\"']", l):
                c = reg.get(name.upper())
                if c is not None:
                    m |= CLASSES.get(c.__module__ + ":" + c.__qualname__, 0)
    except Exception as e:
        log("registry check error", repr(e))
        return None
    return m

def _body_start(fn):
    """First line of a function's body (after decorators, signature and defaults,
    which run at import time and are handled by the import-time rules)."""
    b = fn.body[0]
    return min([b.lineno] + [d.lineno for d in getattr(b, "decorator_list", [])])

def _scope_sig(fn):
    """What Python decides once for a whole function: generator or not, which names
    are local, global or nonlocal. Changing it in a branch affects every call."""
    locs, decl, gen = set(), set(), 0
    todo = list(fn.body)
    while todo:
        n = todo.pop()
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
            if not isinstance(n, ast.Lambda):
                locs.add(n.name)
            # decorators, defaults and bases run in this scope
            todo.extend(getattr(n, "decorator_list", []) or [])
            a = getattr(n, "args", None)
            if a is not None:
                todo.extend(a.defaults); todo.extend(x for x in a.kw_defaults if x is not None)
            todo.extend(getattr(n, "bases", []) or [])
            continue
        if isinstance(n, (ast.Yield, ast.YieldFrom)):
            gen |= 1
        if isinstance(n, ast.Await):
            gen |= 2
        if isinstance(n, (ast.MatchAs, ast.MatchStar)) and n.name:
            locs.add(n.name)
        if isinstance(n, ast.MatchMapping) and n.rest:
            locs.add(n.rest)
        if isinstance(n, (ast.Global, ast.Nonlocal)):
            decl.update((type(n).__name__, x) for x in n.names)
        if isinstance(n, ast.Name) and isinstance(n.ctx, (ast.Store, ast.Del)):
            locs.add(n.id)
        if isinstance(n, ast.alias):
            locs.add((n.asname or n.name).split(".")[0])
        if isinstance(n, (ast.ExceptHandler,)) and n.name:
            locs.add(n.name)
        todo.extend(ast.iter_child_nodes(n))
    return (frozenset(locs), frozenset(decl), gen)

def _functions(tree):
    out = {}
    def walk(node, prefix):
        for ch in ast.iter_child_nodes(node):
            if isinstance(ch, (ast.FunctionDef, ast.AsyncFunctionDef)):
                q = prefix + ch.name
                k = 0
                while (q if k == 0 else f"{q}#{k}") in out:
                    k += 1
                out[q if k == 0 else f"{q}#{k}"] = ch
                walk(ch, q + ".")
            elif isinstance(ch, ast.ClassDef):
                walk(ch, prefix + ch.name + ".")
            else:
                walk(ch, prefix)
    walk(tree, "")
    return out

def scope_changed_lines(old_text, new_text):
    """Every line of each function whose scope signature changed."""
    to = parse_cached(old_text)
    try:
        tn = ast.parse(new_text)
    except SyntaxError:
        return None
    if to is None:
        return None
    fo, fn_ = _functions(to), _functions(tn)
    out = set()
    for q, node in fo.items():
        other = fn_.get(q)
        if other is not None and _scope_sig(node) != _scope_sig(other):
            out.update(range(_body_start(node), (node.end_lineno or node.lineno) + 1))
    return out

_KW = re.compile(r"^(\s*)(if|elif|else|for|while|try|except|finally|with|return|yield|break|continue|raise|match|case)\b")

_FLOW = {"if", "elif", "else", "for", "while", "try", "except", "finally", "with",
         "return", "yield", "break", "continue", "raise", "match", "case", "pass", "await"}

def _struct_sig(lines):
    out = []
    try:
        src = "\n".join(lines) + "\n"
        for t in tokenize.generate_tokens(io.StringIO(src).readline):
            if t.type == tokenize.NAME and t.string in _FLOW:
                line = lines[t.start[0] - 1] if t.start[0] - 1 < len(lines) else ""
                lead = len(line) - len(line.lstrip())
                out.append((len(line[:lead].expandtabs()), t.start[1] == lead, t.string))
    except Exception:
        return None
    return out

def structure_changed_lines(old_text, new_text, hunks):
    """Every line of each function whose control-flow skeleton changed (an elif turned
    into if, a statement moved out of a guard, a return removed...), compared function
    by function so unrelated edits cannot cancel out. Also any line whose indentation
    changed."""
    to = parse_cached(old_text)
    try:
        tn = ast.parse(new_text)
    except SyntaxError:
        return None
    if to is None:
        return None
    ol, nl = old_text.split("\n"), new_text.split("\n")
    fo, fn_ = _functions(to), _functions(tn)
    out = set()
    for q, node in fo.items():
        other = fn_.get(q)
        if other is None:
            continue
        a = ol[node.lineno - 1:node.end_lineno]
        b = nl[other.lineno - 1:other.end_lineno]
        sa, sb = _struct_sig(a), _struct_sig(b)
        if sa is None or sb is None or sa != sb:
            out.update(range(_body_start(node), (node.end_lineno or node.lineno) + 1))
    # indentation moves: the same text removed at one depth and added at another
    rem = {}
    for ostart, body in hunks:
        o = ostart
        for tag, line in body:
            if tag == "-":
                rem.setdefault(line.strip(), []).append((o, len(line.expandtabs()) - len(line.expandtabs().lstrip())))
            if tag != "+":
                o += 1
    for ostart, body in hunks:
        for tag, line in body:
            if tag == "+" and line.strip() in rem:
                ind = len(line.expandtabs()) - len(line.expandtabs().lstrip())
                for o, oind in rem[line.strip()]:
                    if oind != ind:
                        for q, node in fo.items():
                            if node.lineno <= o <= (node.end_lineno or node.lineno):
                                out.update(range(_body_start(node), (node.end_lineno or node.lineno) + 1))
    # a hunk whose removed and added code sits at different depths (moved and edited)
    ind_ = lambda l: len(l.expandtabs()) - len(l.expandtabs().lstrip())
    for ostart, body in hunks:
        ri = sorted(ind_(l) for t, l in body if t == "-" and l.strip() and not l.strip().startswith("#"))
        ai = sorted(ind_(l) for t, l in body if t == "+" and l.strip() and not l.strip().startswith("#"))
        if ri and ai and ri != ai:
            o = ostart
            for t, l in body:
                if t == "-":
                    for q, node in fo.items():
                        if node.lineno <= o <= (node.end_lineno or node.lineno):
                            out.update(range(_body_start(node), (node.end_lineno or node.lineno) + 1))
                if t != "+":
                    o += 1
    return out

_ENUMS = None
def enum_classes():
    """Names of Enum classes defined in sqlglot (from the source)."""
    global _ENUMS
    if _ENUMS is None:
        _ENUMS = set()
        for f in (SRC / "sqlglot").rglob("*.py"):
            try:
                for m in re.finditer(r"^\s*class (\w+)\(([^)]*)\)", f.read_text(), re.M):
                    if re.search(r"\b(Enum|IntEnum|StrEnum|AutoName|Flag|IntFlag)\b", m.group(2)):
                        _ENUMS.add(m.group(1))
            except Exception:
                pass
    return _ENUMS

def _toks(line):
    try:
        return [(t.type, t.string) for t in tokenize.generate_tokens(io.StringIO(line.strip() + "\n").readline)
                if t.type not in (tokenize.NEWLINE, tokenize.NL, tokenize.ENDMARKER, tokenize.COMMENT, tokenize.INDENT, tokenize.DEDENT)]
    except Exception:
        return None

_CMP = {"is", "==", "!=", "<", ">", "<=", ">=", "in"}
_AFTER = (")", ",", ":", "and", "or", "else", "if", "]", "}")

def _safe_operand(seq):
    """seq starts at the operand: None, or EnumClass.MEMBER (optionally module.EnumClass.MEMBER)."""
    if seq[:1] == [(tokenize.NAME, "None")]:
        return len(seq) == 1 or seq[1][1] in _AFTER
    names, k = [], 0
    while k < len(seq) and seq[k][0] == tokenize.NAME:
        names.append(seq[k][1])
        if k + 1 < len(seq) and seq[k + 1][1] == "." and k + 2 < len(seq) and seq[k + 2][0] == tokenize.NAME:
            k += 2
        else:
            k += 1
            break
    if len(names) >= 2 and names[-2] in enum_classes() and re.fullmatch(r"[A-Z][A-Z0-9_]*", names[-1]):
        return k >= len(seq) or seq[k][1] in _AFTER
    return False

def _identity_pair(a, b):
    ta, tb = _toks(a), _toks(b)
    if ta is None or tb is None:
        return False
    if sum(1 for t in ta if t[1] in _CMP) != 1 or sum(1 for t in tb if t[1] in _CMP) != 1:
        return False  # exactly one comparison: no chains
    ia = [k for k, t in enumerate(ta) if t == (tokenize.NAME, "is")]
    ib = [k for k, t in enumerate(tb) if t == (tokenize.NAME, "is")]
    if bool(ia) == bool(ib):
        return False
    to_identity = not ia  # the change introduces `is` (old line used ==)
    if not ia:
        ta, tb, ia = tb, ta, ib
    i = ia[0]
    neg = i + 1 < len(ta) and ta[i + 1] == (tokenize.NAME, "not")
    rest = ta[i + (2 if neg else 1):]
    if ta[:i] + [(tokenize.OP, "!=" if neg else "==")] + rest != tb:
        return False
    if rest[:1] == [(tokenize.NAME, "None")] or (i >= 1 and ta[i - 1] == (tokenize.NAME, "None")):
        return _safe_operand(rest) or rest[:1] != [(tokenize.NAME, "None")]
    # enum members: only `is` -> `==` (a plain string or int equal to a str/int enum
    # member compares == but not `is`, so the other direction can change results)
    return (not to_identity) and _safe_operand(rest)

def _norm_identity(tree, enum_is_to_eq):
    E = enum_classes()
    def is_enum_member(n):
        return (isinstance(n, ast.Attribute) and re.fullmatch(r"[A-Z][A-Z0-9_]*", n.attr)
                and ((isinstance(n.value, ast.Name) and n.value.id in E)
                     or (isinstance(n.value, ast.Attribute) and n.value.attr in E)))
    for n in ast.walk(tree):
        if isinstance(n, ast.Compare) and len(n.ops) == 1:
            a, b = n.left, n.comparators[0]
            none = any(isinstance(x, ast.Constant) and x.value is None and type(x.value) is type(None) for x in (a, b))
            if none:
                if isinstance(n.ops[0], ast.Is):
                    n.ops[0] = ast.Eq()
                elif isinstance(n.ops[0], ast.IsNot):
                    n.ops[0] = ast.NotEq()
            elif enum_is_to_eq and (is_enum_member(a) or is_enum_member(b)):
                if isinstance(n.ops[0], ast.Is):
                    n.ops[0] = ast.Eq()
                elif isinstance(n.ops[0], ast.IsNot):
                    n.ops[0] = ast.NotEq()
    return ast.dump(tree)

def identity_ast_equal(old_text, new_text):
    """Whole-file check: the trees differ, become equal once `is`/`is not` against None
    or an enum member are read as `==`/`!=`, and every changed comparison is either
    against None (any direction) or an enum member going from `is` to `==`. Strings,
    chains and multi-line statements are handled by the parser."""
    try:
        to, tn = ast.parse(old_text), ast.parse(new_text)
        if ast.dump(to) == ast.dump(tn):
            return False
        if _norm_identity(ast.parse(old_text), True) != _norm_identity(ast.parse(new_text), True):
            return False
        E = enum_classes()
        changed = 0
        for a, b in zip(ast.walk(to), ast.walk(tn)):
            if isinstance(a, ast.cmpop) and isinstance(b, ast.cmpop):
                continue  # compared through their Compare node below
            if type(a) is not type(b):
                return False
            if isinstance(a, ast.Compare) and [type(o) for o in a.ops] != [type(o) for o in b.ops]:
                if len(a.ops) != 1:
                    return False
                oa, ob = type(a.ops[0]), type(b.ops[0])
                x, y = a.left, a.comparators[0]
                none = any(isinstance(z, ast.Constant) and z.value is None for z in (x, y))
                if none:
                    pass
                elif (oa, ob) in ((ast.Is, ast.Eq), (ast.IsNot, ast.NotEq)):
                    pass  # enum member, `is` -> `==` (normalisation already checked the operand)
                else:
                    return False
                changed += 1
        return changed > 0
    except Exception:
        return False

def lambda_body_only_lines(old_text, new_text):
    """If the two versions differ only inside the bodies of existing lambdas, return
    the old lines of the changed lambdas; else None."""
    try:
        to, tn = ast.parse(old_text), ast.parse(new_text)
    except SyntaxError:
        return None
    def strip(tree):
        acc = []
        for n in ast.walk(tree):
            if isinstance(n, ast.Lambda):
                acc.append((n.lineno, n.end_lineno or n.lineno, ast.dump(n.body)))
        for n in ast.walk(tree):
            if isinstance(n, ast.Lambda):
                n.body = ast.Constant(None)
        return ast.dump(tree), acc
    so, lo = strip(to)
    sn, ln_ = strip(tn)
    if so != sn or len(lo) != len(ln_):
        return None
    out = set()
    for (s, e, bo), (_, _, bn) in zip(lo, ln_):
        if bo != bn:
            out.update(range(s, e + 1))
    return out or None

def identity_equality_only(hunks):
    """True when every change only swaps `is`/`is not` for `==`/`!=` against None or a
    member of one of sqlglot's Enum classes, line for line inside each hunk."""
    any_ = False
    for _, body in hunks:
        tags = "".join(t for t, _ in body)
        rem = [l for t, l in body if t == "-"]
        add = [l for t, l in body if t == "+"]
        if len(rem) != len(add):
            return False
        if rem and ("-" * len(rem) + "+" * len(add)) not in tags:
            return False  # removed lines must be directly replaced by the added ones
        for a, b in zip(rem, add):
            ind = lambda l: len(l) - len(l.lstrip())
            if ind(a) != ind(b) or not _identity_pair(a, b):
                return False
            any_ = True
    return any_

def try_lines(text, lns):
    """For changes inside a try statement (handlers, else, finally), every line of the
    try body: tests that entered the try can be affected."""
    tree = parse_cached(text)
    if tree is None:
        return set()
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Try, getattr(ast, "TryStar", ast.Try))):
            s, e = node.lineno, node.end_lineno or node.lineno
            if any(s <= ln <= e for ln in lns):
                out.update(range(s, (node.body[-1].end_lineno or s) + 1))
    return out

_imported = False
def import_ok(changed):
    """Execute every changed module's patched source in a forked child that has the
    original sqlglot imported (cheap: one import for the whole run). False on failure."""
    import types
    ensure_sqlglot()
    sys.stdout.flush(); sys.stderr.flush()
    return _forked_check(changed, types)

def ensure_sqlglot():
    global _imported
    import importlib
    if not _imported:
        sys.path.insert(0, str(SRC))
        import sqlglot  # noqa: F401
        import pkgutil
        for m in pkgutil.walk_packages(sqlglot.__path__, "sqlglot."):
            if m.name.endswith("__main__"):
                continue
            try:
                importlib.import_module(m.name)
            except Exception:
                pass
        _imported = True

def _forked_check(changed, types):
    import gc
    gc.freeze()  # keep the parent's pages shared with the child
    pid = os.fork()
    if pid == 0:
        code = 1
        try:
            import io as _io
            sys.stderr = _io.StringIO()
            import signal
            signal.alarm(20)  # an import-time infinite loop must not hang the run
            items = list(changed.items())
            # modules of descendant dialects build on the changed ones (e.g. pop keys
            # from a parent's dict): run them again on top of the patched modules
            extra = []
            for path in changed:
                s = stem_of(path)
                if s:
                    for dsc in sorted(descendants(s)):
                        for d in ("tokenizers", "typing", "parsers", "generators", "dialects"):
                            q = f"sqlglot/{d}/{dsc}.py"
                            if q not in changed and (SRC / q).exists():
                                extra.append((q, (SRC / q).read_text()))
            seen_q = set(); extra = [x for x in extra if not (x[0] in seen_q or seen_q.add(x[0]))]
            allm = items + extra
            # lenient passes settle the order of inheritance chains; the last must succeed
            seq = [(p_, t_, False) for p_, t_ in allm] * 3 + [(p_, t_, True) for p_, t_ in allm]
            for path, text, strict in seq:  # 1st pass may hit stale order; 2nd pass must succeed
                name = path[:-3].replace("/", ".")
                is_pkg = name.endswith(".__init__")
                name = name.removesuffix(".__init__")
                mod = types.ModuleType(name)
                mod.__file__ = str(SRC / path)
                mod.__package__ = name if is_pkg else name.rpartition(".")[0]
                if is_pkg:
                    mod.__path__ = [str((SRC / path).parent)]
                sys.modules[name] = mod
                parent, _, leaf = name.rpartition(".")
                if parent in sys.modules:
                    setattr(sys.modules[parent], leaf, mod)
                try:
                    exec(compile(text.encode("utf-8"), str(SRC / path), "exec"), mod.__dict__)
                except BaseException:
                    if strict:
                        raise
            # build each affected dialect's components (what tests that loop over all
            # dialects do, e.g. the generator memory test)
            stems = set()
            for path in changed:
                s = stem_of(path)
                if s:
                    stems |= descendants(s)
            if stems:
                from sqlglot.dialects.dialect import Dialect
                for klass in list(Dialect._classes.values()):
                    if klass.__module__.rsplit(".", 1)[-1] in stems:
                        d = klass()
                        d.tokenize("SELECT 1")
                        d.parser()
                        d.generator()
            code = 0
        except BaseException:
            code = 1
        finally:
            os._exit(code)
    _, status = os.waitpid(pid, 0)
    return os.WIFEXITED(status) and os.WEXITSTATUS(status) == 0

def code_tokens(text):
    toks = []
    for t in tokenize.generate_tokens(io.StringIO(text).readline):
        if t.type in (tokenize.COMMENT, tokenize.NL, tokenize.ENCODING):
            continue
        toks.append((t.type, t.string))
    return toks

def main():
    import gc
    gc.disable()  # short batch run: no cyclic garbage worth collecting
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
    TESTIMP["imp_by_mod"] = M.get("imp_by_mod", {}); TESTIMP["testdeps"] = M.get("testdeps", {})
    TESTIMP["test_upper_names"] = M.get("test_upper_names", {})
    NARROW[0] = LINES; NARROW[1] = IMP
    for c, m in CLASSES.items():
        CLASS_BY_MODULE.setdefault(c.split(":")[0] + ":", []).append((c, m))
    counts = [0] * n
    for c, m in CLASSES.items():
        mod, _, q = c.partition(":")
        if mod.startswith("sqlglot.dialects.") and "." not in q:
            i = 0
            while m:
                if m & 1:
                    counts[i] += 1
                m >>= 1; i += 1
    for i in range(n):
        if counts[i] >= 30 and tests[i]["seconds"] > 3:
            WIDE[0] |= 1 << i
    log("wide tests:", [tests[i]["id"] for i in range(n) if WIDE[0] >> i & 1])
    filemask_cache = {}
    def filemask(path):
        if path not in filemask_cache:
            m = 0
            for v in LINES.get(path, {}).values():
                m |= int(v, 16)
            filemask_cache[path] = m
        return filemask_cache[path]
    ms = [round(t["seconds"] * 1000) for t in tests]

    all_workers = split(list(range(n)), ms)
    all_cost = cost(all_workers, ms)
    for patch in sorted((DATA / "commits").glob("*.patch")):
        cid = patch.stem
        try:
            sel = select(patch.read_text(errors="replace"), LINES, IMP, READS, by_file, filemask, ALL, CLASSES)
        except Exception as e:
            log(cid, "error", repr(e)); sel = ALL
        nums = [i for i in range(n) if sel >> i & 1]
        workers = split(nums, ms)
        if cost(workers, ms) >= all_cost:
            workers = all_workers  # running everything costs no more: take the safe option
        print(json.dumps({"commit": cid, "workers": workers}))
        log(cid, len(nums), "tests", len(workers), "workers")

def select(text, LINES, IMP, READS, by_file, filemask, ALL, CLASSES=None):
    sel = 0
    import_checks = {}
    all_new = {}
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
                if ast.dump(ast.parse(old_text, type_comments=True)) == ast.dump(ast.parse(new_text, type_comments=True)):
                    continue  # same syntax tree (quote style, redundant parentheses, trailing commas)
            except (tokenize.TokenError, IndentationError, SyntaxError):
                return ALL
            if not old_text.strip():
                return ALL  # code added to an empty file: no coverage to go on
            lam = lambda_body_only_lines(old_text, new_text)
            if lam is not None:
                # only the bodies of existing lambdas changed: nothing changes at import
                # time; the tests that ran those lambda lines are the ones affected
                fl0 = LINES.get(path, {})
                for x in stmt_starts(old_text, lam) | lam:
                    v = fl0.get(str(x))
                    if v:
                        sel |= int(v, 16)
                import_checks[path] = new_text
                all_new[path] = new_text
                continue
            if identity_equality_only(hunks) and identity_ast_equal(old_text, new_text):
                continue  # `is None` <-> `== None` and `is Enum.MEMBER` <-> `== Enum.MEMBER`
            fl = LINES.get(path, {})
            imp = IMP.get(path, set())
            lns = touched(hunks)
            # a blank or comment-only neighbour stands for the nearest code lines around it
            _ol = old_text.split("\n")
            def _code(k):
                return 1 <= k <= len(_ol) and _ol[k - 1].strip() and not _ol[k - 1].strip().startswith("#")
            fixed = set()
            for k in lns:
                if _code(k) or k < 1 or k > len(_ol):
                    fixed.add(k); continue
                a = k
                while a >= 1 and not _code(a):
                    a -= 1
                b = k
                while b <= len(_ol) and not _code(b):
                    b += 1
                if a >= 1:
                    fixed.add(a)
                if b <= len(_ol):
                    fixed.add(b)
            lns = fixed
            lns |= stmt_starts(old_text, lns)
            lns |= try_lines(old_text, lns)
            # Functions that memoize (a cache filled on first call and reused, e.g. the
            # schema caches shared through setUpClass): the first caller covers the fill
            # branch, later callers only the lookup, so take every line of the function.
            tr_ = parse_cached(old_text)
            if tr_ is not None:
                src_lines = old_text.split("\n")
                for fn_ in ast.walk(tr_):
                    if isinstance(fn_, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        a_, b_ = fn_.lineno, fn_.end_lineno or fn_.lineno
                        if any(a_ <= x <= b_ for x in lns) and "cache" in "\n".join(src_lines[a_ - 1:b_]).lower():
                            lns |= set(range(_body_start(fn_), b_ + 1))
            st_ = structure_changed_lines(old_text, new_text, hunks)
            if st_ is None:
                return ALL
            lns |= st_
            sc = scope_changed_lines(old_text, new_text)
            if sc is None:
                return ALL
            lns |= sc
            kw = lambda ls: sorted(m.group(1) for l in ls
                                   for m in [re.match(r"\s*(if|elif|else)\b", l)] if m)
            if kw(old_lines) != kw(new_lines):
                # the if/elif/else structure changed (e.g. elif -> if): every test that
                # evaluated the first condition of the chain is affected
                lns |= chain_heads(old_text, set(lns))
                tree0 = parse_cached(old_text)
                if tree0 is not None:
                    for nd in ast.walk(tree0):
                        if isinstance(nd, ast.If):
                            e_ = nd.end_lineno or nd.lineno
                            if any(nd.lineno <= x <= e_ + 1 for x in touched(hunks)):
                                lns |= set(range(nd.lineno, (nd.body[0].lineno if nd.body else nd.lineno) + 1))
            # a module losing a top-level name breaks whoever imports it
            on, nn = top_names(old_text), top_names(new_text)
            if on is None or nn is None:
                return ALL
            removed = on - nn
            if removed:
                if path.startswith("sqlglot/dialects/") or not stem_of(path):
                    return ALL
                mod = path[:-3].replace("/", ".")
                files = set(TESTIMP.get("imp_by_mod", {}).get(mod, ()))
                for r in removed:
                    files |= set(TESTIMP.get("imp_by_mod", {}).get(mod + "." + r, ()))
                for tf in test_files_closure(files):
                    sel |= by_file.get(tf, 0)
                import_checks[path] = new_text
            sqln_lines = list(new_lines + old_lines)
            for txt, lnset in ((old_text, set(touched(hunks))), (new_text, set(added_new_lines(f)))):
                tr = parse_cached(txt) if txt is old_text else None
                if tr is None:
                    try:
                        tr = ast.parse(txt)
                    except SyntaxError:
                        tr = None
                if tr is None:
                    continue
                tl = txt.split("\n")
                for nd in ast.walk(tr):
                    if isinstance(nd, (ast.Assign, ast.AnnAssign)):
                        tg = nd.targets if isinstance(nd, ast.Assign) else [nd.target]
                        if any(isinstance(t, ast.Name) and t.id == "_sql_names" for t in tg):
                            e_ = nd.end_lineno or nd.lineno
                            if any(nd.lineno <= x <= e_ for x in lnset):
                                sqln_lines += ["_sql_names"] + tl[nd.lineno - 1:e_]
            new_lines_s, old_lines_s = sqln_lines, []
            if any("_sql_names" in l for l in sqln_lines):
                om = registry_owner_mask([l for l in sqln_lines if "_sql_names" in l or l.strip().startswith(("\"", "'"))], CLASSES)
                if om is None:
                    return ALL
                sel |= om
                # function-name registry: tests that built anonymous functions may now
                # resolve to (or stop resolving to) a real class
                for key, mask in CLASSES.items():
                    if key.endswith((":Anonymous", ":AnonymousAggFunc")):
                        sel |= mask
            all_new[path] = new_text
            if any(ln in imp for ln in lns):
                import_checks[path] = new_text
            # New code added outside any function (a new method, class attribute or
            # dict entry) runs at import time even if its neighbours are runtime-only.
            for ln, q in outside_functions(new_text, added_new_lines(f)).items():
                import_checks[path] = new_text
                if stem_of(path):
                    sel |= import_fallback(path, filemask, by_file, ALL, new_text, ln, CLASSES)
                else:
                    key = path[:-3].replace("/", ".") + ":" + q if q else None
                    if key and key in CLASSES:
                        sel |= CLASSES[key] | (0 if path.startswith("sqlglot/expressions/") else filemask(path))
                    else:
                        return ALL
            done_fb = set()
            for ln in lns:
                if ln in imp:
                    fb_key = (stem_of(path) or enclosing_classes(old_text, ln), changed_attr(old_text, ln))
                    if fb_key in done_fb:
                        continue
                    done_fb.add(fb_key)
                    sel |= import_fallback(path, filemask, by_file, ALL, old_text, ln, CLASSES)
                    if sel == ALL:
                        return ALL
                v = fl.get(str(ln))
                if v:
                    sel |= int(v, 16)
        elif path.startswith("tests/") and path.endswith(".py") and (path in by_file or path in TESTIMP.get("testdeps", {})):
            src = SRC / path
            try:
                if src.exists():
                    ot = src.read_text(); nt = apply(ot, hunks)
                    if code_tokens(ot) == code_tokens(nt):
                        continue  # comment-only edit to a test file
            except Exception:
                pass
            for tf in test_files_closure([path]):
                sel |= by_file.get(tf, 0)
        elif READS.get(path):
            pass  # readers already added above (a file with no credited reader runs all)
        elif path in by_file:
            sel |= by_file[path]
        else:
            return ALL
    # Edits can interact: probe with every changed module, and always when several
    # source files change together.
    if (import_checks or len(all_new) > 1) and sel != ALL and not import_ok(all_new):
        return ALL
    return sel

def enclosing_classes(text, ln):
    """Qualified name of the innermost class whose body (outside any function) holds ln."""
    tree = parse_cached(text)
    if tree is None:
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

_SRC_LINES = {}
_SRC_LOWER = {}
_SRC_TEXT = {}
def _src_lines():
    if not _SRC_LINES:
        for f in (SRC / "sqlglot").rglob("*.py"):
            rel = str(f.relative_to(SRC))
            try:
                txt = f.read_text()
                _SRC_LINES[rel] = txt.split("\n")
                _SRC_LOWER[rel] = txt.lower()
                _SRC_TEXT[rel] = txt
            except Exception:
                pass
    return _SRC_LINES

def changed_attr(text, ln):
    """If ln sits in a class-body statement that sets or mutates a plain attribute
    (NAME = ..., NAME += ..., NAME.pop(...), NAME[k] = ...), return NAME."""
    tree = parse_cached(text)
    if tree is None:
        return None
    for cls in ast.walk(tree):
        if not isinstance(cls, ast.ClassDef):
            continue
        for st in cls.body:
            if not (st.lineno <= ln <= (st.end_lineno or st.lineno)):
                continue
            if isinstance(st, ast.ClassDef):
                continue  # the inner class is visited by ast.walk
            tg = None
            if isinstance(st, ast.Assign) and len(st.targets) == 1:
                tg = st.targets[0]
            elif isinstance(st, (ast.AnnAssign, ast.AugAssign)):
                tg = st.target
            elif isinstance(st, ast.Expr) and isinstance(st.value, ast.Call) and isinstance(st.value.func, ast.Attribute):
                tg = st.value.func.value
            if isinstance(tg, ast.Subscript):
                tg = tg.value
            if isinstance(tg, ast.Name) and re.fullmatch(r"[A-Z][A-Z0-9_]*", tg.id):
                return tg.id
            return None
    return None

_LIT_CACHE = {}
def literal_users(stem, LINES):
    if stem in _LIT_CACHE or LINES is None:
        return _LIT_CACHE.get(stem, 0)
    pat = re.compile(r"[\"']" + re.escape(stem) + r"[\"']", re.I)
    m = 0
    for rel, lines in _src_lines().items():
        if stem_of(rel) == stem or stem.lower() not in _SRC_LOWER[rel]:
            continue
        fl = LINES.get(rel, {})
        for k, l in enumerate(lines):
            if pat.search(l):
                for x in stmt_starts(_SRC_TEXT[rel], {k + 1}) | {k + 1}:
                    v = fl.get(str(x))
                    if v:
                        m |= int(v, 16)
    _LIT_CACHE[stem] = m
    return m

_ATTR_CACHE = {}
def attr_readers_mask(name, LINES, IMP, by_file):
    if name not in _ATTR_CACHE:
        _ATTR_CACHE[name] = _attr_readers_mask(name, LINES, IMP, by_file)
    return _ATTR_CACHE[name]

def _attr_readers_mask(name, LINES, IMP, by_file):
    """Tests that ran a line reading `name` anywhere in sqlglot (outside class-body
    definitions of it), or None if some reader runs at import time (then the value
    feeds other definitions and the broad fallback is needed)."""
    pat = re.compile(r"\b" + re.escape(name) + r"\b", re.I)
    low = name.lower()
    m = 0
    for rel, lines in _src_lines().items():
        if low not in _SRC_LOWER[rel]:
            continue  # cheap whole-file check first
        hits = [k + 1 for k, l in enumerate(lines) if pat.search(l)]
        if not hits:
            continue
        imp = IMP.get(rel, set())
        fl = LINES.get(rel, {})
        tree = None
        for h in hits:
            if h in imp:
                # executed at import time: is it only a definition of `name` itself?
                stripped = lines[h - 1].strip()
                if re.match(r"^" + re.escape(name) + r"\s*(:[^=]*)?=(?!=)", stripped) and len(pat.findall(stripped)) == 1:
                    continue
                return None
            v = fl.get(str(h))
            if v:
                m |= int(v, 16)
            # multi-line statement around the reader
            for x in stmt_starts(_SRC_TEXT[rel], {h}):
                if x in imp:
                    return None
                v = fl.get(str(x))
                if v:
                    m |= int(v, 16)
    for tf in TESTIMP.get("test_upper_names", {}).get(name, ()):
        m |= by_file.get(tf, 0)
    return m

def foreign_write(text, ln):
    """A statement at ln that assigns or mutates something reached through another
    object (a.b.C = ..., a.B.X[k] = ..., x.y.pop(...)) at import time."""
    tree = parse_cached(text)
    if tree is None:
        return True
    best = None
    for st in ast.walk(tree):
        if isinstance(st, ast.stmt) and not isinstance(st, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef, ast.If, ast.For, ast.While, ast.With, ast.Try)):
            if st.lineno <= ln <= (st.end_lineno or st.lineno):
                best = st
    if best is None:
        return False
    tgts = []
    if isinstance(best, ast.Assign):
        tgts = best.targets
    elif isinstance(best, (ast.AugAssign, ast.AnnAssign)):
        tgts = [best.target]
    elif isinstance(best, ast.Expr) and isinstance(best.value, ast.Call) and isinstance(best.value.func, ast.Attribute):
        tgts = [best.value.func.value]
    elif isinstance(best, ast.Delete):
        tgts = best.targets
    for t in tgts:
        while isinstance(t, ast.Subscript):
            t = t.value
        if isinstance(t, ast.Attribute):
            root = t
            while isinstance(root, ast.Attribute):
                root = root.value
            if not (isinstance(root, ast.Name) and root.id in ("self", "cls")):
                return True
    return False

def import_fallback(path, filemask, by_file, ALL, text=None, ln=None, CLASSES=None):
    if text is not None and ln is not None and foreign_write(text, ln):
        return ALL
    stem = stem_of(path)
    if stem is None and text is not None and ln is not None and in_function(text, ln):
        return ALL  # core code that also runs while modules load (e.g. parsing type strings)
    if stem is None:
        if text is not None and CLASSES:
            q = enclosing_classes(text, ln)
            if q:
                key = path[:-3].replace("/", ".") + ":" + q
                if key in CLASSES:
                    # CLASSES holds every test that built this class or a subclass
                    return CLASSES[key] | (0 if path.startswith("sqlglot/expressions/") else filemask(path))
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
    # Slow tests that only construct every dialect's components (e.g. the generator
    # memory test) are left out of this broad fallback; a change that breaks component
    # construction also breaks the dialect's own tests, which stay selected.
    # code elsewhere that names the dialect as a string (e.g. "teradata" passed to a
    # format helper) reads its settings on behalf of other dialects' tests
    for s in descendants(stem):
        m |= literal_users(s, NARROW[0])
    m &= ~WIDE[0]
    # A plain class attribute (WEEK_OFFSET, KEYWORDS, ...) matters only where it is
    # read: narrow to the family's tests that ran a reading line.
    if text is not None and ln is not None and NARROW[0] is not None:
        name = changed_attr(text, ln)
        if name:
            r = attr_readers_mask(name, NARROW[0], NARROW[1], by_file)
            if r is not None:
                return m & r
    return m

def cost(workers, ms):
    if not workers:
        return 0
    return max(sum(ms[i] for i in w) for w in workers) + WORKER_MS * len(workers)

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
