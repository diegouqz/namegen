"""CLI: python -m namegen.cli --region AR --count 5 [--gender female] [--seed x] [--regions]"""
import argparse, json, sys
from .core import NamegenError, generate, list_regions

def main():
    ap = argparse.ArgumentParser(prog="namegen", description="Random region-typical names (JSON output)")
    ap.add_argument("--region", "-r"); ap.add_argument("--count", "-n", type=int, default=1)
    ap.add_argument("--gender", "-g", default="any"); ap.add_argument("--seed")
    ap.add_argument("--regions", action="store_true", help="List supported regions")
    ap.add_argument("--plain", action="store_true", help="One full name per line")
    a = ap.parse_args()
    try:
        out = list_regions() if a.regions else generate(a.region, a.count, a.gender, a.seed)
    except NamegenError as e:
        print(json.dumps(e.to_dict(), ensure_ascii=False), file=sys.stderr); sys.exit(2)
    if a.plain and not a.regions:
        print("\n".join(r["full_name"] for r in out["results"]))
    else:
        print(json.dumps(out, ensure_ascii=False, indent=2))

if __name__ == "__main__":
    main()
