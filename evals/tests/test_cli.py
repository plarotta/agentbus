import json

from omega_eval.cli import main
from omega_eval.registry import load_registry

TASKS = [
    {"task_id": "t1", "benchmark": "bench", "family": "f", "grader": "tests"},
    {"task_id": "t2", "benchmark": "bench", "family": "f", "grader": "tests"},
]
BASE = {"task_id": "t1", "model": "m", "harness": "h", "run_set": "scoring", "attempt": 0}
ROWS = [
    BASE | {"arm": "solo", "passed": False, "cost_usd": 1.0},
    BASE | {"arm": "solo", "task_id": "t2", "passed": True, "cost_usd": 1.0},
    BASE | {"arm": "omega", "harness": "omega", "passed": True, "cost_usd": 1.0},
    BASE | {"arm": "omega", "harness": "omega", "task_id": "t2", "passed": False, "cost_usd": 1.0},
]
REFS = "references:\n  - {benchmark: bench, model: m, expected_rate: 50.0, source: x}\n"


def write_jsonl(path, rows):
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n")


def test_end_to_end(tmp_path, capsys):
    tasks, results = tmp_path / "tasks.jsonl", tmp_path / "results.jsonl"
    db, reg, refs = str(tmp_path / "r.sqlite"), str(tmp_path / "reg.jsonl"), tmp_path / "refs.yaml"
    write_jsonl(tasks, TASKS)
    write_jsonl(results, ROWS)
    refs.write_text(REFS)

    assert main(["registry", "add", str(tasks), "--registry", reg]) == 0
    assert main(["registry", "add", str(tasks), "--registry", reg]) == 0  # identical is fine
    assert sorted(load_registry(reg)) == ["t1", "t2"]
    assert main(["registry", "list", "--registry", reg]) == 0
    assert "t1" in capsys.readouterr().out

    assert main(["results", "import-jsonl", str(results), "--db", db]) == 0
    assert "imported 4" in capsys.readouterr().out

    assert main(["report", "success", "--db", db, "--registry", reg]) == 0
    assert "pass_rate" in capsys.readouterr().out
    assert main(["report", "success", "--db", db, "--registry", reg, "--json"]) == 0
    rows = json.loads(capsys.readouterr().out)
    assert sorted(r["pass_rate"] for r in rows) == [50.0, 50.0]  # solo group and omega group

    args = ["report", "validate", "--db", db, "--registry", reg, "--references", str(refs)]
    assert main(args) == 0
    assert "consistent" in capsys.readouterr().out
    assert main([*args, "--json"]) == 0
    [ref] = json.loads(capsys.readouterr().out)
    assert ref["status"] == "consistent" and ref["measured_rate"] == 50.0

    args = ["report", "compare", "--db", db, "--registry", reg, "--system-arm", "omega"]
    assert main(args) == 0
    assert "novel" in capsys.readouterr().out
    assert main([*args, "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    # t1: omega pass, solo fail -> novel; t2: omega fail, solo pass -> regression
    assert out["counts"] == {"novel": 1, "regression": 1, "both_solved": 0, "neither": 0}
    assert out["net"] == 0


def test_errors_return_2(tmp_path, capsys):
    db = str(tmp_path / "r.sqlite")
    assert main(["results", "import-jsonl", str(tmp_path / "missing.jsonl"), "--db", db]) == 2
    assert capsys.readouterr().err.startswith("error:")
    bad = tmp_path / "bad.jsonl"
    write_jsonl(bad, [ROWS[0] | {"attempt": -1}])
    assert main(["results", "import-jsonl", str(bad), "--db", db]) == 2
    assert "line 1" in capsys.readouterr().err
