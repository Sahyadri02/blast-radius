import json, glob, collections, sys
sys.path.insert(0, "/home/user/ship2")
T = json.load(open("/home/user/br/sample/tests.json"))["tests"]; n = len(T)
ms = [round(t["seconds"] * 1000) for t in T]; idn = {t["id"]: t["number"] for t in T}
ALL = (1 << n) - 1
WMS = 2000
def cost_of(nums):
    if not nums: return 0
    order = sorted(nums, key=lambda i: -ms[i]); best = None
    for k in range(1, min(8, len(nums)) + 1):
        L = [0] * k
        for i in order: j = L.index(min(L)); L[j] += ms[i]
        c = max(L) + WMS * k
        best = c if best is None or c < best else best
    return best / 1000
def bits(m): return [i for i in range(n) if m >> i & 1]
NAMES = {974: "data-file readers", 1023: "lambda rule", 1098: "removed name -> importers",
         1123: "_sql_names owner", 1128: "_sql_names anonymous", 1145: "table rule (added code)",
         1151: "added class code -> CLASSES", 1158: "table rule (import line)", 1169: "line coverage",
         1180: "test-file edit"}
SUB = {"fb1563": "fallback: foreign write ALL", "fb1567": "fallback: import-only", "fb1570": "fallback: core in-function ALL",
       "fb1578": "fallback: CLASSES", "fb1579": "fallback: core ALL", "fb1606": "fallback: attr-readers narrowed",
       "fb1607": "fallback: dialect family"}
ALLN = {989: "ALL: syntax/compile", 1092: "ALL: removed name in dialect/core", 1153: "ALL: added core code, state changed",
        1166: "ALL: import-time fallback", 1190: "ALL: import probe failed"}
def label(line, sub):
    if line in (1147, 1164): return SUB.get(sub, f"fallback {sub}") + (" [added code]" if line == 1147 else "")
    if sub == "table_mask": return NAMES.get(line, str(line))
    return NAMES.get(line, f"site {line}")
rows = []
for f in sorted(glob.glob("trace_*.jsonl")):
    s = f[6:-6]
    outs = {json.loads(l)["commit"]: json.loads(l) for l in open(f"out_orig_{s}.jsonl")}
    if s == "sample":
        A = json.load(open("/home/user/br/sample/answers.json"))["commits"]; fails = {c: set(a["failing"]) for c, a in A.items()}
    else:
        fails = {}
        for c in outs:
            fails[c] = {idn[x.strip()] for x in open(f"/home/user/ws/dev/mut/{s}/truth/{c}.txt") if x.strip() in idn}
    for l in open(f):
        d = json.loads(l); c = d["commit"]
        w = outs[c]["workers"]; sel = set(x for ww in w for x in ww)
        cost = (max(sum(ms[i] for i in ww) for ww in w) + WMS * len(w)) / 1000 if w else 0
        orc = cost_of(sorted(fails[c])); waste = cost - orc
        assert fails[c] <= sel, ("MISS", s, c)
        slowfail = any(ms[i] >= 2000 for i in fails[c])
        groups = collections.defaultdict(int)
        sub_before_all = None
        for line, sub, m in d["sites"]:
            mm = int(m, 16)
            if line in (1147, 1164) and sub: sub_before_all = sub
            if mm: groups[label(line, sub)] |= mm
        if d["all"]:
            prim = ALLN.get(d["all"], f"ALL {d['all']}")
            if d["all"] == 1166 and sub_before_all: prim += " (" + SUB.get(sub_before_all, sub_before_all) + ")"
            sav = {prim: waste}
        else:
            fm = int(d["final"], 16) if "final" in d else 0
            sav = {}
            for g, m in groups.items():
                others = 0
                for g2, m2 in groups.items():
                    if g2 != g: others |= m2
                excl = m & ~others & ~sum(1 << i for i in fails[c])
                if excl & fm:
                    sav[g] = cost - cost_of(sorted(set(bits(fm & ~excl)) | fails[c])) if cost else 0
            if not sav and groups:
                sav = {max(groups, key=lambda g: cost_of(bits(groups[g]))): waste}
            prim = max(sav, key=sav.get) if sav else "nothing selected"
        rows.append(dict(set=s, commit=c, cost=cost, orc=orc, waste=waste, prim=prim, sav=sav, slowfail=slowfail,
                         nsel=len(sel), nfail=len(fails[c])))
json.dump(rows, open("rows.json", "w"))
tot = collections.defaultdict(float); cnt = collections.Counter(); exs = collections.defaultdict(float)
for r in rows:
    tot[r["prim"]] += r["waste"]; cnt[r["prim"]] += 1
    for g, v in r["sav"].items(): exs[g] += v
print(f"commits {len(rows)}  total cost {sum(r['cost'] for r in rows):.1f}  oracle {sum(r['orc'] for r in rows):.1f}  waste {sum(r['waste'] for r in rows):.1f}")
print(f"{'rule (charged the commit waste)':55} {'commits':>7} {'waste s':>9} {'exclusive-saving s':>18}")
for g in sorted(set(tot) | set(exs), key=lambda g: -tot.get(g, 0)):
    print(f"{g:55} {cnt[g]:7} {tot.get(g,0):9.1f} {exs.get(g,0):18.1f}")
print("\nTop 10 by waste among commits with no failing slow test (>= 2 s):")
for r in sorted([r for r in rows if not r["slowfail"]], key=lambda r: -r["waste"])[:10]:
    print(f"  {r['set']:7} {r['commit']:12} cost {r['cost']:6.1f} oracle {r['orc']:5.1f} waste {r['waste']:6.1f} sel {r['nsel']:5} fail {r['nfail']:3}  <- {r['prim']}")
