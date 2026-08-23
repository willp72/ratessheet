"""Run the parsers against Will's saved pages to prove they work."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from rates import parse_hl, parse_meteor, parse_raisin, parse_flagstone_isa, top_ten, render_text, BUCKETS

U = "/mnt/user-data/uploads/"
def read(f): return open(U+f, encoding="utf-8", errors="replace").read()

products = []
products += parse_hl(read("hargreaves1.html"), False)
products += parse_hl(read("hargreaves2.html"), True)
products += parse_meteor(read("Easy_Access___Save_with_confidence__Invest_with_impact_.html"), False, "easy-access")
products += parse_raisin(read("Compare_top_fixed_rate_bonds__Up_to_4_81__AER___Raisin_UKa.html"), "fixed")
products += parse_flagstone_isa(read("flagstone_2.html"))

from collections import Counter
print("by source:", Counter(p.source for p in products))
print("by bucket:", Counter(p.bucket for p in products))
print()
ranked = top_ten(products)
print(render_text(ranked))
print("=== provenance check (12m) ===")
for p in ranked["12m"]:
    print(f"  {p.rate:.2f}  {p.bank[:32]:34} {p.source:10} {p.term_raw}")
