"""New map keys for tracked sets and scalar settings (run in the mapgen dir after runmap.sh).

usage: buildmap2.py tests.json OLD_MAP.json.gz NEW_MAP.json OUT_MAP.json.gz
Checks that the fresh run's coverage keys equal NEW_MAP's (the standard buildmap.py output
of the same run) and that its dict-tracking keys equal OLD_MAP's, then writes OLD_MAP plus:
  sacc    {name: {key: hex mask}}  tests reading set element / whole set ("*") / scalar ("v")
  sphase  {name: {key: [phase]}}   reads outside tests (warmup, warmuse, other, collect:<file>)
  salias  {walk name: canonical set name}
  sleaky  [canonical set names]    referenced elsewhere, rebound or changed while tests ran
  vdefs   [definer.ATTR]           scalar settings that were tracked
  vleaky  [definer.ATTR]           scalar settings rebound while tests ran
Scalar names in sacc: "v:<lookup class key>.<ATTR>" and "d:<definer key>.<ATTR>".
"""
import gzip, json, sys

tests = json.load(open(sys.argv[1]))["tests"]
num = {t["id"]: t["number"] for t in tests}
OLD = json.load(gzip.open(sys.argv[2]))
NEW = json.load(open(sys.argv[3]))

for k in ("lines", "classes"):
    assert OLD[k] == NEW[k], f"coverage key {k} differs from the current map"
for k in ("tacc", "tphase", "talias", "tleaky", "tderived"):
    assert OLD[k] == NEW[k], f"dict-tracking key {k} differs from the current map"


def resolve(c):
    if c in num:
        return 1 << num[c]
    if c.startswith("COLLECT::"):
        f = c[9:]
        m = 0
        for k, n in num.items():
            if k.split("::")[0] == f:
                m |= 1 << n
        return m or None
    if c.startswith("SETUP::"):
        pre = c[7:] + "::"
        m = 0
        for k, n in num.items():
            if k.startswith(pre):
                m |= 1 << n
        return m or None
    return None


_rc = {}


def R(c):
    if c not in _rc:
        _rc[c] = resolve(c)
    return _rc[c]


sacc, sphase, salias = {}, {}, {}
sleaky, vdefs, vleaky = set(), set(), set()
for i in range(4):
    A = json.load(open(f"out{i}.acc2.json"))
    salias.update(A["ealias"])
    sleaky |= set(A["eleaky"])
    vdefs |= set(A["vdefs"])
    vleaky |= set(A["vleaky"])
    for c, lst in A["acc"].items():
        ph = None
        if "::" in c and not c.startswith(("SETUP::", "COLLECT::")) and c not in num:
            continue  # skipped test
        if c == "WARMUSE":
            ph = "warmuse"
        elif c == "WARMUP":
            ph = "warmup"
        elif c.startswith("COLLECT::"):
            ph = "collect:" + c[9:]
        elif c == "" or c == "IMPORT":
            ph = "other"
        m = R(c) if ph is None or ph.startswith("collect:") else None
        if ph is None and m is None:
            ph = "other"
        for n, k in lst:
            if m:
                d = sacc.setdefault(n, {})
                d[k] = d.get(k, 0) | m
            if ph:
                sphase.setdefault(n, {}).setdefault(k, set()).add(ph)

out = dict(OLD)
out["sacc"] = {n: {k: format(m, "x") for k, m in d.items()} for n, d in sacc.items()}
out["sphase"] = {n: {k: sorted(v) for k, v in d.items()} for n, d in sphase.items()}
out["salias"] = salias
out["sleaky"] = sorted(sleaky)
out["vdefs"] = sorted(vdefs)
out["vleaky"] = sorted(vleaky)
with gzip.open(sys.argv[4], "wt") as fh:
    json.dump(out, fh, separators=(",", ":"))
print("sets", len(set(salias.values())), "leaky", len(sleaky), "scalars", len(vdefs), "vleaky", len(vleaky),
      "sacc names", len(sacc))
