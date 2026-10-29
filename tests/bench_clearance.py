"""Time clearance_search.py on a fixed set of marks and check that its results haven't changed.

  python3 -I tests/bench_clearance.py record OUT_DIR   run the marks, save each result as JSON
  python3 -I tests/bench_clearance.py compare BASE_DIR  run the marks again, compare with BASE_DIR

Uses TMC_DB (default data/uspto.db). A mark passes when it returns the same searches (same match
counts) and the same candidates, in the same order. The search keeps the K most similar marks of each
search; a tie at the cut-off can legitimately pick a different one of the tied marks, so a candidate
that differs only by being tied with the last one kept is reported as a tie, not a failure. "identical"
means every field of every candidate matches, goods and statement text included.
"""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SEARCH = Path(os.environ.get("TMC_SEARCH", ROOT / "search" / "clearance_search.py"))  # to time another copy
MARKS = [("BLUE HERON", "009,042"), ("STAR", "009,035"), ("APPLE", "009"), ("GOWAVE", "009,042"),
         ("SMART HOME HUB", "009,042"), ("KWIK STOP", "035"), ("NIKE", ""), ("LUMINA", "009,042"),
         ("PHOENIX", "035,041"), ("THE WASHINGTON HERALD", "009,041")]


def run(mark, classes):
    cmd = [sys.executable, "-I", str(SEARCH), "clearance", "--mark", mark]
    if classes:
        cmd += ["--classes", classes]
    start = time.time()
    out = subprocess.run(cmd, capture_output=True, text=True, check=True).stdout
    return json.loads(out), time.time() - start


def slug(mark, classes):
    return f"{mark.replace(' ', '_')}__{classes or 'all'}.json"


def main():
    if len(sys.argv) != 3 or sys.argv[1] not in ("record", "compare"):
        sys.exit(__doc__)
    mode, directory = sys.argv[1], Path(sys.argv[2])
    if mode == "record":
        directory.mkdir(parents=True, exist_ok=True)
    total, bad = 0.0, 0
    print(f"{'mark':26} {'classes':8} {'secs':>6}  result")
    for mark, classes in MARKS:
        result, secs = run(mark, classes)
        total += secs
        verdict = "saved"
        if mode == "record":
            (directory / slug(mark, classes)).write_text(json.dumps(result, indent=1))
        else:
            base = json.loads((directory / slug(mark, classes)).read_text())
            same_searches = [(s["search"], s.get("matches"), s.get("kept")) for s in base["searches_run"]] == \
                            [(s["search"], s.get("matches"), s.get("kept")) for s in result["searches_run"]]
            a = [c["serial_number"] for c in base["candidates"]]
            b = [c["serial_number"] for c in result["candidates"]]
            same_candidates = a == b
            tie_only = (not same_candidates and base["candidates_found"] == result["candidates_found"] and
                        sorted(c["similarity"] for c in base["candidates"]) ==
                        sorted(c["similarity"] for c in result["candidates"]))
            same_records = base["candidates"] == result["candidates"]  # every field, goods and statements text included
            verdict = "identical" if same_searches and same_records else \
                      "same candidates, text differs" if same_searches and same_candidates else \
                      "same scores, different tied marks" if same_searches and tie_only else "DIFFERENT"
            bad += verdict in ("DIFFERENT", "same candidates, text differs")
        print(f"{mark:26} {classes or 'all':8} {secs:6.1f}  {verdict}  "
              f"({result['candidates_found']} candidates)")
    print(f"total {total:.1f}s" + (f", {bad} different" if mode == "compare" else ""))
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()
