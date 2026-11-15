import json

import pytest
from omega_eval.importers import import_cmu_summary, import_inspect_log, import_jsonl
from omega_eval.pricing import Price

BASE = {
    "task_id": "t1",
    "arm": "solo",
    "model": "m",
    "harness": "h",
    "run_set": "scoring",
    "attempt": 0,
    "passed": True,
}


def write(tmp_path, rows):
    p = tmp_path / "in.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    return p


def test_jsonl_with_and_without_cost(tmp_path):
    prices = {"m": Price(1.0, 3.0)}
    p = write(
        tmp_path,
        [
            BASE | {"cost_usd": 0.25},
            BASE | {"attempt": 1, "input_tokens": 1_000_000, "output_tokens": 1_000_000},
        ],
    )
    got = import_jsonl(p, prices)
    # second: 1e6 * 1.0 / 1e6 + 1e6 * 3.0 / 1e6 = 4.0
    assert [a.cost_usd for a in got] == [0.25, 4.0]


def test_missing_cost_stays_unknown(tmp_path):
    # No cost, and either no prices or no tokens: unknown, never a guessed zero.
    p = write(tmp_path, [BASE | {"cost_usd": None}, BASE | {"attempt": 1}])
    assert [a.cost_usd for a in import_jsonl(p)] == [None, None]
    assert [a.cost_usd for a in import_jsonl(p, {"m": Price(1.0, 3.0)})] == [None, None]


def test_unpriced_model_reports_line(tmp_path):
    p = write(tmp_path, [BASE | {"cost_usd": 0.1}, BASE | {"attempt": 1, "input_tokens": 10}])
    with pytest.raises(ValueError, match="line 2"):
        import_jsonl(p, {"other": Price(1.0, 3.0)})


def test_validation_error_reports_line(tmp_path):
    p = write(tmp_path, [BASE | {"cost_usd": 0.1, "attempt": -1}])
    with pytest.raises(ValueError, match="line 1"):
        import_jsonl(p)


def test_inspect_log(tmp_path):
    pytest.importorskip("inspect_ai")
    from inspect_ai import Task, eval
    from inspect_ai.dataset import Sample
    from inspect_ai.model import ModelOutput, ModelUsage, get_model
    from inspect_ai.scorer import exact
    from inspect_ai.solver import generate

    outputs = []
    for text in ("4", "wrong"):
        out = ModelOutput.from_content("mockllm/model", text)
        out.usage = ModelUsage(input_tokens=1_000_000, output_tokens=500_000)
        outputs.append(out)
    task = Task(
        dataset=[Sample(input="2+2", target="4", id="a"), Sample(input="3+3", target="6", id="b")],
        solver=generate(),
        scorer=exact(),
    )
    model = get_model("mockllm/model", custom_outputs=outputs)
    [log] = eval(task, model=model, log_dir=str(tmp_path / "logs"), display="none")
    assert log.status == "success"

    prices = {"mockllm/model": Price(1.0, 2.0)}
    got = import_inspect_log(
        log.location,
        arm="solo",
        harness="h",
        run_set="screening",
        prices=prices,
        task_id_prefix="p:",
    )
    got.sort(key=lambda a: a.task_id)
    assert [(a.task_id, a.passed, a.attempt) for a in got] == [("p:a", True, 0), ("p:b", False, 0)]
    # 1e6 * 1.0 / 1e6 + 5e5 * 2.0 / 1e6 = 2.0 per sample
    assert [a.cost_usd for a in got] == [pytest.approx(2.0)] * 2
    assert got[0].model == "mockllm/model"
    assert (got[0].input_tokens, got[0].output_tokens) == (1_000_000, 500_000)

    with pytest.raises(KeyError):  # an unpriced model is an error, never a silent zero
        import_inspect_log(log.location, arm="solo", harness="h", run_set="screening", prices={})
    excluded = import_inspect_log(
        log.location,
        arm="solo",
        harness="h",
        run_set="screening",
        prices={},
        exclude_models=("mockllm/model",),
    )
    assert [(a.cost_usd, a.input_tokens) for a in excluded] == [(0.0, 0)] * 2


def cmu_record(example_id, *, split="test", score=1.0, cost=0.5, rollout_id=0):
    return {
        "example_id": example_id,
        "rollout_id": rollout_id,
        "split": split,
        "workspace_dir": f"ws/example{example_id}_rollout{rollout_id}",
        "score": score,
        "cost": cost,
        "end_to_end_latency_sec": 12.5,
        "prompt_tokens": 100,
        "completion_tokens": 20,
    }


def cmu_row(task_id="budget_approval_s2l_high", model="m", version="baseline", val=0.5, **kw):
    return {
        "task_id": task_id,
        "model_name": model,
        "prompt_name": "p",
        "rollout_version": version,
        "status": kw.pop("status", "ok"),
        "val": {"score": val},
        "workspace_records": kw.pop("records", [cmu_record(42)]),
    }


def write_cmu(tmp_path, rows):
    p = tmp_path / "summary.json"
    p.write_text(json.dumps({"rows": rows}))
    return p


def test_cmu_mapping_and_filters(tmp_path):
    p = write_cmu(
        tmp_path,
        [
            cmu_row(
                records=[
                    cmu_record(42, score=0.4, cost=None),
                    cmu_record(43, score=1.0, rollout_id=2),
                    cmu_record(44, score=None),
                    cmu_record(5, split="train"),
                    cmu_record(25, split="val"),
                ]
            ),
            cmu_row(task_id="unknown_task"),
            cmu_row(task_id="webtest", status="error"),
        ],
    )
    attempts, tasks = import_cmu_summary(p)
    partial, full, null_score = attempts
    assert partial.task_id == "budget-approval-ex042"
    assert (partial.score, partial.passed, partial.cost_usd) == (0.4, False, None)
    assert (full.score, full.passed, full.attempt, full.cost_usd) == (1.0, True, 2, 0.5)
    assert (null_score.score, null_score.passed) == (0.0, False)
    assert (partial.arm, partial.model, partial.harness) == ("solo", "m", "openhands-sdk-default")
    assert (partial.run_set, partial.input_tokens, partial.output_tokens) == ("screening", 100, 20)
    assert partial.wall_seconds == 12.5
    assert partial.trajectory == "ws/example42_rollout0" and partial.source == str(p)
    assert partial.settings == {"split": "test", "rollout_version": "baseline", "prompt_name": "p"}
    assert [(t.task_id, t.benchmark, t.family, t.grader, t.split) for t in tasks] == [
        (f"budget-approval-ex{n:03d}", "cmu-business-tasks", "budget-approval", "mixed", "test")
        for n in (42, 43, 44)
    ]


def test_cmu_optimized_selects_best_rows(tmp_path):
    p = write_cmu(
        tmp_path,
        [
            cmu_row(version="baseline", records=[cmu_record(42)]),
            cmu_row(version="gepa_seed1_best", records=[cmu_record(43)]),
        ],
    )
    [base], _ = import_cmu_summary(p)
    [tuned], _ = import_cmu_summary(p, optimized=True, run_set="scoring")
    assert (base.task_id, base.harness) == ("budget-approval-ex042", "openhands-sdk-default")
    assert (tuned.task_id, tuned.harness) == ("budget-approval-ex043", "openhands-sdk-adapted")
    assert tuned.run_set == "scoring"


def test_cmu_best_validation_row_wins_per_task_and_model(tmp_path):
    p = write_cmu(
        tmp_path,
        [
            cmu_row(version="baseline", val=0.3, records=[cmu_record(42)]),
            cmu_row(version="baseline_rerun_20260504", val=0.7, records=[cmu_record(43)]),
            cmu_row(model="other", val=0.1, records=[cmu_record(44)]),
        ],
    )
    got = {(a.model, a.task_id) for a in import_cmu_summary(p)[0]}
    assert got == {("m", "budget-approval-ex043"), ("other", "budget-approval-ex044")}


def test_cmu_models_filter(tmp_path):
    p = write_cmu(tmp_path, [cmu_row(model="m"), cmu_row(model="other")])
    assert {a.model for a in import_cmu_summary(p, models=["other"])[0]} == {"other"}
    assert {a.model for a in import_cmu_summary(p)[0]} == {"m", "other"}
    assert import_cmu_summary(p, models=[]) == ([], [])
