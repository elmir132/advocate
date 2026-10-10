import json, os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from score import score

TRUTH = json.load(open(os.path.join(os.path.dirname(__file__), "..", "data", "landlord_lines.json")))


def test_perfect_run_scores_one():
    res = [{"id": t["id"], "flags": t["expected_flags"], "promise": t["promise"]} for t in TRUTH]
    s = score(TRUTH, res)
    assert s["micro_precision"] == 1.0 and s["micro_recall"] == 1.0 and s["promise_accuracy"] == 1.0


def test_empty_run_has_zero_recall_and_flags_nothing():
    s = score(TRUTH, [])
    assert s["micro_recall"] == 0.0 and s["micro_precision"] is None
    assert s["promise_accuracy"] == round(sum(not t["promise"] for t in TRUTH) / len(TRUTH), 3)


def test_unknown_flags_are_ignored_and_extra_flags_hurt_precision():
    res = [{"id": t["id"], "flags": t["expected_flags"] + (["penalty"] if "penalty" not in t["expected_flags"] else []) + ["made_up"], "promise": t["promise"]} for t in TRUTH]
    s = score(TRUTH, res)
    assert s["micro_recall"] == 1.0 and s["micro_precision"] < 1.0
