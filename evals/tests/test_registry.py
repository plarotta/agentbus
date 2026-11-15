import pytest
from omega_eval.registry import TaskRecord, load_registry, save_registry


def rec(task_id: str, **kw) -> TaskRecord:
    return TaskRecord(task_id=task_id, benchmark="b", grader="tests", **kw)


def test_round_trip_sorted(tmp_path):
    path = tmp_path / "tasks.jsonl"
    records = {"b2": rec("b2", family="f", vetted=True), "a1": rec("a1", split="test")}
    save_registry(path, records)
    assert [line.split('"')[3] for line in path.read_text().splitlines()] == ["a1", "b2"]
    assert load_registry(path) == records


def test_duplicate_rejected(tmp_path):
    path = tmp_path / "tasks.jsonl"
    line = rec("x").model_dump_json()
    path.write_text(f"{line}\n{line}\n")
    with pytest.raises(ValueError, match="x"):
        load_registry(path)


def test_missing_file_is_empty(tmp_path):
    assert load_registry(tmp_path / "nope.jsonl") == {}
