"""Score the advocate against data/landlord_lines.json.

  python3 score.py results.json

results.json: [{"id": "L01", "flags": ["penalty", ...], "promise": false}, ...]
Flags are the category names listed in ADVOCATE_PROMPT.md. A missing id counts as no flags.
Reports per-flag precision and recall, overall micro precision and recall, and promise accuracy.
"""
import json, sys
from collections import Counter

CATS = ["financial_term", "penalty", "vague_commitment", "promise", "restriction"]


def score(truth, results):
    got = {r["id"]: r for r in results}
    tp, fp, fn = Counter(), Counter(), Counter()
    promise_ok = 0
    for t in truth:
        r = got.get(t["id"], {"flags": [], "promise": False})
        exp, pred = set(t["expected_flags"]), {f for f in r.get("flags", []) if f in CATS}
        for c in CATS:
            if c in exp and c in pred: tp[c] += 1
            elif c in pred: fp[c] += 1
            elif c in exp: fn[c] += 1
        promise_ok += bool(r.get("promise")) == bool(t["promise"])
    def pr(a, b): return round(a / (a + b), 3) if a + b else None
    per = {c: {"precision": pr(tp[c], fp[c]), "recall": pr(tp[c], fn[c])} for c in CATS}
    return {"lines": len(truth), "micro_precision": pr(sum(tp.values()), sum(fp.values())),
            "micro_recall": pr(sum(tp.values()), sum(fn.values())),
            "promise_accuracy": round(promise_ok / len(truth), 3), "per_flag": per}


if __name__ == "__main__":
    truth = json.load(open(__file__.rsplit("/", 1)[0] + "/data/landlord_lines.json")) if "/" in __file__ else json.load(open("data/landlord_lines.json"))
    print(json.dumps(score(truth, json.load(open(sys.argv[1]))), indent=2))
