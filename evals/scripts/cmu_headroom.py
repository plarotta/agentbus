"""Pool oracle and headroom on the Carnegie Mellon authors' published per-example records.

Usage: python cmu_headroom.py <replication_package/outputs/data/summary.json>

Reads that one JSON file and prints counts of test examples per task. An example counts
as solved by a model when any of its three attempts scored 1.0. "Pool" is the three small
models; the default harness is the baseline run, the adapted harness is the optimized run
with the best validation score (the authors' own selection rule).
"""

import collections
import json
import sys
from pathlib import Path

SMALL = ["gemma-4-26b-a4b", "qwen3-coder-30b-a3b", "ministral-3-8b"]
FRONTIER = "gemini-3.1-pro-preview"
TASKS = {
    "attendance_payroll_audit_s2l_high": "attendance-auditing",
    "budget_approval_s2l_high": "budget-approval",
    "woocommerce_stock_alert_s2l": "stock-alert",
    "machine_operating_s2l": "anomaly-detection",
    "webtest": "playwright-testing",
    "webarena": "website-management",
    "refactorbench": "code-refactoring",
}
rows = [
    r
    for r in json.loads(Path(sys.argv[1]).read_text())["rows"]
    if r["status"] == "ok" and r["task_id"] in TASKS
]


def pick(task, model, optimized):
    c = [
        r
        for r in rows
        if r["task_id"] == task
        and r["model_name"] == model
        and r["rollout_version"].endswith("best") == optimized
    ]
    return max(c, key=lambda r: r["val"]["score"]) if c else None


def per_example(row):
    """example_id -> list of (score, cost) on the test split."""
    out = collections.defaultdict(list)
    for w in row["workspace_records"]:
        if w["split"] == "test":
            out[w["example_id"]].append((w["score"] or 0.0, w["cost"] or 0.0))
    return out


def solved(ex):  # any attempt fully correct
    return {e for e, a in ex.items() if any(s >= 0.999 for s, _ in a)}


out = []
tot = collections.Counter()
for task, name in TASKS.items():
    base = {m: per_example(pick(task, m, False)) for m in SMALL}
    adapt = {m: per_example(pick(task, m, True)) for m in SMALL}
    front = per_example(pick(task, FRONTIER, False))
    ids = set(front)
    assert all(set(v) == ids for v in list(base.values()) + list(adapt.values())), task
    n = len(ids)
    sb = {m: solved(base[m]) for m in SMALL}
    sa = {m: solved(adapt[m]) for m in SMALL}
    ob, oa = set().union(*sb.values()), set().union(*sa.values())
    both = ob | oa
    sf = solved(front)
    rec = {
        "task": name,
        "n_test": n,
        "base_any3": {m: len(sb[m]) for m in SMALL},
        "base_best_single": max(len(v) for v in sb.values()),
        "base_pool_oracle": len(ob),
        "adapt_any3": {m: len(sa[m]) for m in SMALL},
        "adapt_best_single": max(len(v) for v in sa.values()),
        "adapt_pool_oracle": len(oa),
        "pool_oracle_either": len(both),
        "unsolved_by_pool_either": n - len(both),
        "of_those_frontier_solves": len(sf - both),
        "frontier_any3": len(sf),
        "pool_solves_frontier_misses": len(both - sf),
    }
    out.append(rec)
    for k in (
        "n_test",
        "base_best_single",
        "base_pool_oracle",
        "adapt_best_single",
        "adapt_pool_oracle",
        "pool_oracle_either",
        "unsolved_by_pool_either",
        "of_those_frontier_solves",
        "frontier_any3",
        "pool_solves_frontier_misses",
    ):
        tot[k] += rec[k]
json.dump({"tasks": out, "total": tot}, sys.stdout, indent=1)
