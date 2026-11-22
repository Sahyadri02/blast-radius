import re, sys
src = open(sys.argv[1]).read().split("\n")
out = []
fn = None
for i, l in enumerate(src, 1):
    m = re.match(r"^def (\w+)", l)
    if m: fn = m.group(1)
    s = l
    if fn == "select":
        s = re.sub(r"^(\s*)sel \|= (.*)$", lambda m: f"{m.group(1)}sel |= _T({i}, {m.group(2)})", s)
        s = re.sub(r"^(\s*)return ALL(\s*(#.*)?)$", lambda m: f"{m.group(1)}return _TA({i}){m.group(2)}", s)
        s = re.sub(r"^(\s*)return sel$", lambda m: f"{m.group(1)}return _TEND(sel)", s)
    if fn == "import_fallback":
        s = re.sub(r"^(\s*)return (.+?)(\s*#.*)?$", lambda m: f"{m.group(1)}return _F({i}, {m.group(2)})", s)
    if fn == "_tab":
        pass
    out.append(s)
txt = "\n".join(out)
txt = txt.replace("                return table_mask()", "                return _TM(table_mask())")
txt = txt.replace("    for patch in sorted((DATA / \"commits\").glob(\"*.patch\")):\n        cid = patch.stem\n",
                  "    for patch in sorted((DATA / \"commits\").glob(\"*.patch\")):\n        cid = patch.stem\n        TRACE.clear(); TRACE.append(cid)\n")
helpers = '''
TRACE = []
_LASTF = [None]
_LASTTM = [False]
_TRF = open(os.environ["TRACE_OUT"], "a")
def _T(line, m):
    sub = None
    if _LASTF[0] is not None:
        sub = _LASTF[0]; _LASTF[0] = None
    if _LASTTM[0]:
        sub = "table_mask"; _LASTTM[0] = False
    TRACE.append([line, sub, format(m, "x") if isinstance(m, int) else "0"])
    return m
def _TA(line):
    _TRF.write(json.dumps({"commit": TRACE[0], "all": line, "sites": TRACE[1:]}) + "\\n"); _TRF.flush()
    return ALLMASK[0]
def _TEND(sel):
    _TRF.write(json.dumps({"commit": TRACE[0], "all": None, "sites": TRACE[1:], "final": format(sel, "x")}) + "\\n"); _TRF.flush()
    return sel
def _F(line, m):
    _LASTF[0] = "fb%d" % line
    return m
def _TM(m):
    if m is not None:
        _LASTTM[0] = True
    return m
ALLMASK = [0]
'''
txt = txt.replace("def main():", helpers + "\ndef main():", 1)
txt = txt.replace("    ALL = (1 << n) - 1\n", "    ALL = (1 << n) - 1\n    ALLMASK[0] = ALL\n", 1)
open(sys.argv[2], "w").write(txt)
