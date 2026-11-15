import os
import tempfile
from pathlib import Path
from typing import Literal

from pydantic import BaseModel


class TaskRecord(BaseModel):
    task_id: str
    benchmark: str
    benchmark_version: str = ""
    family: str = ""
    structure: Literal["decomposable", "sequential", "mixed", "unknown"] = "unknown"
    grader: Literal["tests", "state_check", "exact_match", "model_judge", "mixed"]
    split: Literal["unassigned", "screening", "development", "test"] = "unassigned"
    vetted: bool = False
    notes: str = ""


def load_registry(path: str | Path) -> dict[str, TaskRecord]:
    path = Path(path)
    if not path.exists():
        return {}
    records: dict[str, TaskRecord] = {}
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        rec = TaskRecord.model_validate_json(line)
        if rec.task_id in records:
            raise ValueError(f"duplicate task_id in registry: {rec.task_id}")
        records[rec.task_id] = rec
    return records


def save_registry(path: str | Path, records: dict[str, TaskRecord]) -> None:
    path = Path(path)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name, suffix=".tmp")
    with os.fdopen(fd, "w") as f:
        for task_id in sorted(records):
            f.write(records[task_id].model_dump_json() + "\n")
    os.replace(tmp, path)
