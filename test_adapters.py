"""Run: python test_adapters.py <dir with sample files>. Checks every parser ties to known totals."""
import sys, glob, adapters as A
from collections import defaultdict
U = sys.argv[1] if len(sys.argv) > 1 else "."
g = lambda p: open(glob.glob(U + "/" + p)[0], errors="replace").read()
def tot(rows, pred, party):
    t = defaultdict(int)
    for r in rows:
        if pred(r) and r["party"] == party: t[r["method"]] += r["votes"]
    return dict(t)
cases = [
 ("clarity pinal Lake", tot(A.parse_clarity(g("*pinal_az_summary.json"), g("*pinal_az_vt.json")), lambda r: r["contest"] == "U.S. Senator", "R"), 115595),
 ("brevard csv Trump", tot(A.parse_vr_csv(g("*brevard_fl_cand*")), lambda r: r["contest"].startswith("President"), "R"), 216533),
 ("brevard html Trump", tot(A.parse_vr_html(g("*brevard_fl_summary.html")), lambda r: r["contest"] == "President and Vice President", "R"), 216533),
 ("tx anderson Trump", tot(A.parse_tx(g("*tx_county*"))["48001"], lambda r: r["contest"].startswith("PRESIDENT"), "R"), 15597),
 ("nc beaufort Trump", tot(A.parse_nc(g("*beaufort*")), lambda r: r["contest"].startswith("US PRESIDENT"), "R"), 17296),
]
ok = True
for n, t, want in cases:
    s = sum(t.values()) - t.get("provisional", 0) * (n.startswith("clarity") or n.startswith("nc")) + 0
    s = sum(t.values()); good = s == want; ok &= good
    print(("PASS" if good else "FAIL"), n, t, "sum", s, "expected", want)
sys.exit(0 if ok else 1)
