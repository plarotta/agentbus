import pytest
from omega_eval.results import Attempt, ResultsStore


def mk(task_id="t", attempt=0, **kw) -> Attempt:
    base = dict(
        task_id=task_id,
        arm="solo",
        model="m",
        harness="h",
        run_set="scoring",
        attempt=attempt,
        passed=True,
        cost_usd=0.5,
    )
    return Attempt(**(base | kw))


def test_round_trip_with_settings(tmp_path):
    a = mk(settings={"retriever": "bm25", "n": 3, "t": 0.5, "flag": True}, passed=False)
    with ResultsStore(tmp_path / "r.sqlite") as store:
        store.add([a])
        assert store.query() == [a]


def test_unknown_cost_round_trips_as_none(tmp_path):
    with ResultsStore(tmp_path / "r.sqlite") as store:
        store.add([mk(cost_usd=None)])
        assert store.query()[0].cost_usd is None


def test_duplicate_rejected_and_nothing_inserted(tmp_path):
    with ResultsStore(tmp_path / "r.sqlite") as store:
        store.add([mk("t", 0)])
        with pytest.raises(ValueError, match="duplicate"):
            store.add([mk("t", 1), mk("t", 0)])
        assert [a.attempt for a in store.query()] == [0]


def test_query_filters_and_order(tmp_path):
    with ResultsStore(tmp_path / "r.sqlite") as store:
        store.add(
            [
                mk("b", 0),
                mk("a", 1),
                mk("a", 0),
                mk("a", 0, arm="omega", model="x", harness="y"),
                mk("c", 0, run_set="screening"),
            ]
        )
        assert [(a.task_id, a.attempt) for a in store.query(arm="solo", run_set="scoring")] == [
            ("a", 0),
            ("a", 1),
            ("b", 0),
        ]
        assert len(store.query(model="x")) == 1
        assert len(store.query(harness="y")) == 1
        assert len(store.query(run_set="screening")) == 1


def test_negative_attempt_and_cost_rejected():
    with pytest.raises(ValueError):
        mk(attempt=-1)
    with pytest.raises(ValueError):
        mk(cost_usd=-0.1)
