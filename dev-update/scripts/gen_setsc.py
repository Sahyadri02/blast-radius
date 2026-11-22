"""Mutants for set-element and scalar-setting changes.

usage: gen_setsc.py SEED OUTDIR WORKTREE
Writes ~60 patches (OUTDIR/<kind>_<nn>.patch) made against a clean WORKTREE:
  se_*  remove one element of a set display used in a class attribute / module global
  sa_*  add an element (taken from another set display in the same file) to a set
  sc_*  change a scalar UPPER_CASE class setting (bool flip, int +-1, str edit)
  so_*  add an override of an inherited scalar setting to a dialect-family class
  sr_*  remove a scalar override from a dialect-family class
  ea_*  flip a True/False value in an expression class's arg_types (changes required_args)
Every patch must still compile; patches are spread over parser.py, tokens.py,
generator.py, dialects/*, parsers/*, generators/*, tokenizers/* and expressions/*.
"""
import ast, glob, os, random, re, subprocess, sys

SEED, OUT, WT = int(sys.argv[1]), sys.argv[2], sys.argv[3]
rng = random.Random(SEED)
os.makedirs(OUT, exist_ok=True)
os.chdir(WT)
UP = re.compile(r"^[A-Z][A-Z0-9_]*$")

GROUPS = {
    "parser": ["sqlglot/parser.py"],
    "tokens": ["sqlglot/tokens.py"],
    "generator": ["sqlglot/generator.py"],
    "dialects": sorted(glob.glob("sqlglot/dialects/*.py") + glob.glob("sqlglot/parsers/*.py")
                       + glob.glob("sqlglot/generators/*.py") + glob.glob("sqlglot/tokenizers/*.py")),
    "expressions": sorted(glob.glob("sqlglot/expressions/*.py")),
}
# (kind, group) -> how many patches
PLAN = {
    ("se", "parser"): 5, ("se", "tokens"): 3, ("se", "generator"): 2, ("se", "dialects"): 5, ("se", "expressions"): 3,
    ("sa", "parser"): 4, ("sa", "tokens"): 2, ("sa", "generator"): 1, ("sa", "dialects"): 3, ("sa", "expressions"): 2,
    ("sc", "parser"): 4, ("sc", "tokens"): 3, ("sc", "generator"): 4, ("sc", "dialects"): 7,
    ("so", "dialects"): 5, ("sr", "dialects"): 3,
    ("ea", "expressions"): 6,
}


def src_of(f):
    return open(f).read()


def class_level_values(tree):
    """(ClassDef or None, stmt) for assignments directly in a class body or at module level."""
    for st in tree.body:
        if isinstance(st, (ast.Assign, ast.AnnAssign)):
            yield None, st
    for cls in ast.walk(tree):
        if isinstance(cls, ast.ClassDef):
            for st in cls.body:
                if isinstance(st, (ast.Assign, ast.AnnAssign)):
                    yield cls, st


def target_name(st):
    tg = st.targets if isinstance(st, ast.Assign) else [st.target]
    if len(tg) == 1 and isinstance(tg[0], ast.Name):
        return tg[0].id
    return None


def set_displays(f):
    tree = ast.parse(src_of(f))
    out = []
    for cls, st in class_level_values(tree):
        if st.value is None:
            continue
        for nd in ast.walk(st.value):
            if isinstance(nd, ast.Set):
                els = [e for e in nd.elts if not isinstance(e, ast.Starred) and e.lineno == e.end_lineno]
                if els:
                    out.append((st, nd, els))
    return out


def seg(lines, e):
    return lines[e.lineno - 1][e.col_offset:e.end_col_offset]


def mut_set_remove(f, lines):
    cands = []
    for st, nd, els in set_displays(f):
        named = [e for e in els if len(nd.elts) >= 2]
        cands += [(nd, e) for e in named]
    rng.shuffle(cands)
    for nd, e in cands:
        line = lines[e.lineno - 1]
        s = seg(lines, e)
        if line.strip() in (s + ",", s):
            return lines[:e.lineno - 1] + lines[e.lineno:], f"remove {s}"
        # inline: drop "s, " or ", s"
        i, j = e.col_offset, e.end_col_offset
        rest = line[j:]
        if rest.startswith(", "):
            return lines[:e.lineno - 1] + [line[:i] + rest[2:]] + lines[e.lineno:], f"remove {s}"
        if line[:i].endswith(", "):
            return lines[:e.lineno - 1] + [line[:i - 2] + rest] + lines[e.lineno:], f"remove {s}"
    return None


def mut_set_add(f, lines):
    disp = set_displays(f)
    pool = sorted({seg(lines, e) for _, _, els in disp for e in els})
    cands = list(disp)
    rng.shuffle(cands)
    for st, nd, els in cands:
        have = {seg(lines, e) for e in nd.elts if not isinstance(e, ast.Starred)}
        prefix = seg(lines, els[0]).split(".")[0]
        choices = [p for p in pool if p not in have and p.split(".")[0] == prefix and "(" not in p]
        if not choices:
            continue
        new = rng.choice(choices)
        e = rng.choice(els)
        line = lines[e.lineno - 1]
        s = seg(lines, e)
        if line.strip() in (s + ",",):
            ind = line[:len(line) - len(line.lstrip())]
            return lines[:e.lineno] + [ind + new + ","] + lines[e.lineno:], f"add {new}"
        i = e.col_offset
        return lines[:e.lineno - 1] + [line[:i] + new + ", " + line[i:]] + lines[e.lineno:], f"add {new}"
    return None


def scalar_stmts(f, upper_only=True):
    tree = ast.parse(src_of(f))
    out = []
    for cls, st in class_level_values(tree):
        if cls is None:
            continue
        n = target_name(st)
        v = st.value
        if not n or (upper_only and not UP.match(n)) or v is None or st.lineno != st.end_lineno:
            continue
        if isinstance(v, ast.UnaryOp) and isinstance(v.op, ast.USub) and isinstance(v.operand, ast.Constant):
            out.append((cls, st, n, -v.operand.value))
        elif isinstance(v, ast.Constant) and isinstance(v.value, (bool, int, str)):
            out.append((cls, st, n, v.value))
    return out


def new_scalar(val):
    if isinstance(val, bool):
        return repr(not val)
    if isinstance(val, int):
        return repr(val + rng.choice((1, -1)))
    if isinstance(val, str):
        if not val:
            return repr("X")
        return repr(val[:-1] if len(val) > 1 else val + "X")
    return None


def mut_scalar(f, lines):
    cands = scalar_stmts(f)
    rng.shuffle(cands)
    for cls, st, n, val in cands:
        nv = new_scalar(val)
        if nv is None:
            continue
        line = lines[st.lineno - 1]
        v = st.value
        nl = line[:v.col_offset] + nv + line[v.end_col_offset:]
        return lines[:st.lineno - 1] + [nl] + lines[st.lineno:], f"{cls.name}.{n} {seg(lines, v)} -> {nv}"
    return None


def base_scalars():
    """Scalar settings defined on the core Dialect/Parser/Generator/Tokenizer classes."""
    out = {}
    for f, cname in (("sqlglot/dialects/dialect.py", "Dialect"), ("sqlglot/parser.py", "Parser"),
                     ("sqlglot/generator.py", "Generator"), ("sqlglot/tokens.py", "Tokenizer")):
        for cls, st, n, val in scalar_stmts(f):
            if cls.name == cname:
                out.setdefault(cname, {})[n] = val
    return out


def family(cls_node, f):
    """Which core class a dialect-family class derives from (by its base names)."""
    names = [ast.unparse(b) for b in cls_node.bases]
    for b in names:
        for fam in ("Parser", "Generator", "Tokenizer", "Dialect"):
            if b.endswith(fam) or b.split(".")[-1] == fam:
                return fam
    if f.startswith("sqlglot/dialects/") and any(b in ("Dialect",) or b[:1].isupper() for b in names) \
            and cls_node.name not in ("Tokenizer", "Parser", "Generator"):
        return "Dialect"
    return None


def mut_override(f, lines):
    base = base_scalars()
    tree = ast.parse(src_of(f))
    cands = []
    for cls in ast.walk(tree):
        if not isinstance(cls, ast.ClassDef) or not cls.body:
            continue
        fam = family(cls, f)
        if fam not in base:
            continue
        own = {target_name(st) for st in cls.body if isinstance(st, (ast.Assign, ast.AnnAssign))}
        for n, val in base[fam].items():
            if n not in own:
                cands.append((cls, n, val))
    rng.shuffle(cands)
    for cls, n, val in cands:
        nv = new_scalar(val)
        if nv is None:
            continue
        first = cls.body[0]
        at = first.end_lineno  # after the docstring / first statement
        ind = " " * first.col_offset
        return lines[:at] + ["", ind + f"{n} = {nv}"] + lines[at:], f"{cls.name} +{n} = {nv}"
    return None


def mut_remove_override(f, lines):
    base = base_scalars()
    tree = ast.parse(src_of(f))
    cands = []
    for cls in ast.walk(tree):
        if not isinstance(cls, ast.ClassDef):
            continue
        fam = family(cls, f)
        if fam not in base:
            continue
        for st in cls.body:
            n = target_name(st) if isinstance(st, (ast.Assign, ast.AnnAssign)) else None
            if n in base[fam] and st.lineno == st.end_lineno and len(cls.body) > 1:
                cands.append((cls, st, n))
    rng.shuffle(cands)
    for cls, st, n in cands:
        return lines[:st.lineno - 1] + lines[st.lineno:], f"{cls.name} -{n}"
    return None


def mut_argtypes(f, lines):
    tree = ast.parse(src_of(f))
    cands = []
    for cls, st in class_level_values(tree):
        if cls is None or target_name(st) != "arg_types" or not isinstance(st.value, ast.Dict):
            continue
        for k, v in zip(st.value.keys, st.value.values):
            if isinstance(v, ast.Constant) and isinstance(v.value, bool) and v.lineno == v.end_lineno:
                cands.append((cls, k, v))
    rng.shuffle(cands)
    for cls, k, v in cands:
        line = lines[v.lineno - 1]
        nl = line[:v.col_offset] + repr(not v.value) + line[v.end_col_offset:]
        return lines[:v.lineno - 1] + [nl] + lines[v.lineno:], f"{cls.name}.arg_types[{seg(lines, k)}] -> {not v.value}"
    return None


KINDS = {"se": mut_set_remove, "sa": mut_set_add, "sc": mut_scalar, "so": mut_override,
         "sr": mut_remove_override, "ea": mut_argtypes}

made = 0
seen = set()
for (kind, group), n in PLAN.items():
    files = list(GROUPS[group])
    got = 0
    tries = 0
    while got < n and tries < 400:
        tries += 1
        f = rng.choice(files)
        src = src_of(f)
        lines = src.split("\n")
        r = KINDS[kind](f, lines)
        if r is None:
            continue
        new_lines, what = r
        new = "\n".join(new_lines)
        if new == src or (f, what) in seen:
            continue
        try:
            compile(new, f, "exec")
        except SyntaxError:
            continue
        seen.add((f, what))
        open(f, "w").write(new)
        p = subprocess.run(["git", "diff"], capture_output=True, text=True).stdout
        subprocess.run(["git", "checkout", "-q", "--", "."])
        name = f"{kind}_{group[:3]}{got:02d}"
        open(f"{OUT}/{name}.patch", "w").write(p)
        print(name, f, what)
        got += 1
        made += 1
print("made", made, file=sys.stderr)
