"""Mutants that change one constant default argument of a function or method.

usage: gen_defaults.py SEED OUTDIR WORKTREE
Patches df_<group><nn>.patch: a positional or keyword-only default that is a bool / int /
str / None constant on a single line is changed (bool flipped, int +-1, str edited,
None -> a value of the annotated kind or False). Spread over the core modules,
expressions, optimizer, dialect-family files and helpers.
"""
import ast, glob, os, random, subprocess, sys

SEED, OUT, WT = int(sys.argv[1]), sys.argv[2], sys.argv[3]
rng = random.Random(SEED)
os.makedirs(OUT, exist_ok=True)
os.chdir(WT)
GROUPS = {
    "par": (["sqlglot/parser.py"], 7),
    "gen": (["sqlglot/generator.py"], 7),
    "tok": (["sqlglot/tokenizer_core.py", "sqlglot/tokens.py"], 4),
    "exp": (sorted(glob.glob("sqlglot/expressions/*.py")), 8),
    "opt": (sorted(glob.glob("sqlglot/optimizer/*.py")), 8),
    "dia": (sorted(glob.glob("sqlglot/dialects/*.py") + glob.glob("sqlglot/parsers/*.py")
                   + glob.glob("sqlglot/generators/*.py")), 10),
    "oth": (["sqlglot/helper.py", "sqlglot/schema.py", "sqlglot/transforms.py", "sqlglot/time.py",
             "sqlglot/__init__.py", "sqlglot/lineage.py", "sqlglot/diff.py", "sqlglot/planner.py"], 6),
}


def cands(f):
    tree = ast.parse(open(f).read())
    out = []
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        a = fn.args
        pos = a.posonlyargs + a.args
        pairs = list(zip(pos[len(pos) - len(a.defaults):], a.defaults)) + \
            [(k, d) for k, d in zip(a.kwonlyargs, a.kw_defaults) if d is not None]
        for arg, d in pairs:
            if isinstance(d, ast.Constant) and d.lineno == d.end_lineno and isinstance(d.value, (bool, int, str, type(None))):
                out.append((fn, arg, d))
    return out


def new_value(arg, d):
    v = d.value
    if isinstance(v, bool):
        return repr(not v)
    if isinstance(v, int):
        return repr(v + rng.choice((1, -1)))
    if isinstance(v, str):
        return repr(v[:-1] if len(v) > 1 else v + "X")
    ann = ast.unparse(arg.annotation) if arg.annotation is not None else ""
    if "bool" in ann:
        return "True"
    if "int" in ann:
        return "1"
    if "str" in ann:
        return repr("x")
    return "False"


seen = set()
made = 0
for g, (files, n) in GROUPS.items():
    files = [f for f in files if os.path.exists(f)]
    got = tries = 0
    while got < n and tries < 500:
        tries += 1
        f = rng.choice(files)
        cs = cands(f)
        if not cs:
            continue
        fn, arg, d = rng.choice(cs)
        if (f, d.lineno, d.col_offset) in seen:
            continue
        src = open(f).read()
        lines = src.split("\n")
        line = lines[d.lineno - 1]
        nv = new_value(arg, d)
        nl = line[:d.col_offset] + nv + line[d.end_col_offset:]
        new = "\n".join(lines[:d.lineno - 1] + [nl] + lines[d.lineno:])
        try:
            compile(new, f, "exec")
        except SyntaxError:
            continue
        seen.add((f, d.lineno, d.col_offset))
        open(f, "w").write(new)
        p = subprocess.run(["git", "diff"], capture_output=True, text=True).stdout
        subprocess.run(["git", "checkout", "-q", "--", "."])
        name = f"df_{g}{got:02d}"
        open(f"{OUT}/{name}.patch", "w").write(p)
        print(name, f, f"{fn.name}({arg.arg}={ast.unparse(d)} -> {nv})")
        got += 1
        made += 1
print("made", made, file=sys.stderr)
