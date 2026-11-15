import pytest
from omega_eval.analysis import compare, oracle_solves, success_table, validate
from omega_eval.registry import TaskRecord
from omega_eval.results import Attempt


def mk(task_id, attempt=0, passed=False, cost=1.0, **kw) -> Attempt:
    base = dict(
        task_id=task_id,
        arm="solo",
        model="m",
        harness="h",
        run_set="scoring",
        attempt=attempt,
        passed=passed,
        cost_usd=cost,
    )
    return Attempt(**(base | kw))


def reg(**families: str) -> dict[str, TaskRecord]:
    return {
        t: TaskRecord(task_id=t, benchmark="bench", family=f, grader="tests")
        for t, f in families.items()
    }


def test_oracle_budget_equal_included():
    solo = [mk("t", 0), mk("t", 1, passed=True), mk("t", 2)]
    assert oracle_solves(solo, "t", 2.0)  # cumulative 1.0 + 1.0 == budget
    assert not oracle_solves(solo, "t", 1.99)  # only the first attempt fits


def test_oracle_first_attempt_always_taken():
    assert oracle_solves([mk("t", 0, passed=True, cost=5.0)], "t", 1.0)


def test_oracle_pass_beyond_budget_ignored():
    solo = [mk("t", 0), mk("t", 1), mk("t", 2, passed=True)]
    assert not oracle_solves(solo, "t", 2.0)  # third attempt would reach 3.0
    assert oracle_solves(solo, "t", 3.0)


def test_oracle_any_group_and_task_filter():
    solo = [mk("t", 0), mk("t", 0, model="other", passed=True), mk("u", 0, passed=True)]
    assert oracle_solves(solo, "t", 1.0)
    assert not oracle_solves([mk("t", 0), mk("u", 0, passed=True)], "t", 10.0)


def test_compare_counts():
    registry = reg(t1="f", t2="f", t3="g", t4="g")
    solo = [mk("t1"), mk("t2", passed=True), mk("t3", passed=True), mk("t4")]
    system = [
        mk("t1", 0, True, arm="omega"),  # novel
        mk("t1", 1, True, arm="omega"),  # novel
        mk("t2", 0, False, arm="omega"),  # regression
        mk("t3", 0, True, arm="omega"),  # both solved
        mk("t4", 0, False, arm="omega"),  # neither
    ]
    r = compare(system, solo, registry, n_boot=200)
    assert r["counts"] == {"novel": 2, "regression": 1, "both_solved": 1, "neither": 1}
    assert r["by_family"]["f"] == {"novel": 2, "regression": 1, "both_solved": 0, "neither": 0}
    assert r["by_family"]["g"] == {"novel": 0, "regression": 0, "both_solved": 1, "neither": 1}
    assert r["net"] == 1
    assert r["n_trials"] == 5
    assert r["net_per_trial"] == pytest.approx(0.2)
    assert r["ci_low"] <= r["ci_high"]


def test_compare_rejects_screening_and_wrong_arm_and_missing_solo():
    registry = reg(t1="f")
    system = [mk("t1", arm="omega")]
    with pytest.raises(ValueError, match="screening"):
        compare(system, [mk("t1", run_set="screening")], registry)
    with pytest.raises(ValueError, match="system-arm"):
        compare(system, [mk("t1", arm="agentbus")], registry)
    with pytest.raises(ValueError, match="solo-arm"):
        compare([mk("t1")], [mk("t1")], registry)
    with pytest.raises(ValueError, match="t1"):
        compare(system, [mk("other")], registry)


def test_compare_refuses_unknown_cost():
    registry = reg(t1="f")
    with pytest.raises(ValueError, match="unknown cost"):
        compare([mk("t1", arm="omega", cost=None)], [mk("t1")], registry)
    with pytest.raises(ValueError, match="unknown cost"):
        compare([mk("t1", arm="omega")], [mk("t1", cost=None)], registry)


def test_success_table_mean_over_tasks_not_attempts():
    registry = reg(t1="f", t2="f")
    attempts = [
        mk("t1", 0, True),
        mk("t2", 0, True),
        mk("t2", 1),
        mk("t2", 2),
        mk("t2", 3),
    ]
    [row] = success_table(attempts, registry)
    # tasks: t1 = 1.0, t2 = 0.25 -> 62.5%; mean over attempts would be 40%
    assert row["pass_rate"] == pytest.approx(62.5)
    assert (row["n_tasks"], row["n_attempts"]) == (2, 5)
    assert row["ci_low"] <= 62.5 <= row["ci_high"]
    with pytest.raises(KeyError, match="zzz"):
        success_table([mk("zzz")], registry)


def test_validate_statuses_and_settings_subset():
    tasks = [f"t{i}" for i in range(40)]
    registry = reg(**dict.fromkeys(tasks, "f"))
    attempts = [mk(t, passed=True, settings={"r": "low", "x": 1}) for t in tasks]
    base = {"benchmark": "bench", "model": "m", "family": "f", "harness": "h"}
    refs = [
        base | {"expected_rate": 100.0},
        base | {"expected_rate": 50.0},  # 40 of 40 passed: interval is about 91.2 to 100
        base | {"expected_rate": 100.0, "benchmark": "other"},
        base | {"expected_rate": 100.0, "settings": {"r": "low"}},  # subset of attempt settings
        base | {"expected_rate": 100.0, "settings": {"r": "high"}},
        base | {"expected_rate": 100.0, "family": "other"},
    ]
    got = validate(attempts, registry, refs)
    assert [r["status"] for r in got] == [
        "consistent",
        "differs",
        "no_data",
        "consistent",
        "no_data",
        "no_data",
    ]
    assert got[0]["measured_rate"] == 100.0
    assert got[0]["n_tasks"] == 40
    assert got[0]["ci_low"] == pytest.approx(91.24, abs=0.01)
    assert got[0]["ci_high"] == 100.0


def test_validate_interval_is_not_degenerate_at_zero():
    # 0 of 60 tasks passed. A bootstrap interval would be exactly [0, 0] and would call a
    # published 1.1% a mismatch. Wilson upper bound: z^2 / (n + z^2) = 3.8415 / 63.8415 = 6.02%.
    tasks = [f"t{i}" for i in range(60)]
    registry = reg(**dict.fromkeys(tasks, "f"))
    attempts = [mk(t) for t in tasks]
    base = {"benchmark": "bench", "model": "m"}
    got = validate(
        attempts, registry, [base | {"expected_rate": 1.1}, base | {"expected_rate": 9.9}]
    )
    assert [r["status"] for r in got] == ["consistent", "differs"]
    assert (got[0]["measured_rate"], got[0]["ci_low"]) == (0.0, 0.0)
    assert got[0]["ci_high"] == pytest.approx(6.02, abs=0.01)
