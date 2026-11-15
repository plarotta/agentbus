import json

import pytest
from omega_eval.importers import import_inspect_log, import_jsonl
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
